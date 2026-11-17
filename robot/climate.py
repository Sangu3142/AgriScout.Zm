"""ClimateMonitor: reads the DHT11 every few seconds in the background and keeps the latest good reading.

Runs inside the robot server (Python 3.7). It never imports pigpio itself: the server hands it two
functions, so hardware.py stays the only file that talks to the GPIO libraries, and tests can pass
fakes:
  connect()         -> (pi, None) or (None, reason)     hardware.connect_pigpio
  open_sensor(pi)   -> an object with read() and close() hardware.Dht11 (powers the module on)

tick(now) is a state machine that takes the time and never sleeps (like auto.py), so the tests run
on fake time. start() calls it every config.CLIMATE_READ_PERIOD_S in a daemon thread.

Rules:
- The first reading after power-on is thrown away (on the tank it was the only one that failed).
- A failed reading keeps the previous good one; its age just grows.
- pigpiod not running, or gone while we run: report available=False with the reason, never crash,
  try again every config.CLIMATE_RETRY_S.
"""

import threading
import time
import traceback

import config

START_PIGPIOD = "sudo pigpiod -l -p %d" % config.PIGPIO_PORT
NOT_RUNNING_REASON = "pigpiod not running - start it with: " + START_PIGPIOD


def _snapshot(available, reading, now, ok, failed, last_error, reason):
    """The /api/status "climate" dict. reading = (temp_c, humidity_pct, read_at) or None."""
    if reading is None:
        temp_c = humidity_pct = age_s = None
    else:
        temp_c, humidity_pct, read_at = reading
        age_s = round(max(0.0, now - read_at), 1)
    return {"available": available, "temp_c": temp_c, "humidity_pct": humidity_pct,
            "age_s": age_s, "ok": ok, "failed": failed, "last_error": last_error,
            "reason": reason}


class DisabledClimate(object):
    """Stands in for the monitor when config.CLIMATE_ENABLED is False (or in tests)."""

    REASON = "climate monitor turned off (config.CLIMATE_ENABLED = False)"

    def start(self):
        pass

    def close(self):
        pass

    def snapshot(self, now):
        return _snapshot(False, None, now, 0, 0, None, self.REASON)


class ClimateMonitor(object):

    def __init__(self, connect, open_sensor, clock=time.monotonic):
        self._connect = connect
        self._open_sensor = open_sensor
        self._clock = clock
        # _io_lock: around open/read/close, so close() never runs in the middle of a read or of the
        # 2 s power-on wait. _data_lock: only around the numbers, so status never waits on I/O.
        self._io_lock = threading.Lock()
        self._data_lock = threading.Lock()
        self._pi = None
        self._sensor = None
        self._discard_next = False
        self._retry_at = None  # None = try at the next tick
        self._closed = False
        self._stop_event = threading.Event()
        self._thread = None

        self._available = False
        self._reason = "starting"
        self._reading = None   # (temp_c, humidity_pct, read_at) of the last GOOD reading
        self._ok = 0
        self._failed = 0
        self._last_error = None

    # ---------- state machine ----------

    def tick(self, now):
        with self._io_lock:
            if self._closed:
                return
            if self._sensor is None:
                if self._retry_at is not None and now < self._retry_at:
                    return
                if not self._open(now):
                    return
            self._read(now)

    def _open(self, now):
        """Connect to pigpiod and power the sensor on. io_lock held. True if it worked."""
        try:
            pi, reason = self._connect()
        except Exception as err:  # connect_pigpio shouldn't raise, but never let it kill us
            pi, reason = None, str(err)
        if pi is None:
            if reason == "pigpiod is not running":
                reason = NOT_RUNNING_REASON
            else:
                reason = "%s (start pigpiod with: %s)" % (reason, START_PIGPIOD)
            self._unavailable(now, reason)
            return False
        try:
            sensor = self._open_sensor(pi)  # Dht11: power pin HIGH, then waits 2 s
        except Exception as err:
            self._stop_pi(pi)
            self._unavailable(now, "cannot open the DHT11: %s" % err)
            return False
        self._pi, self._sensor = pi, sensor
        self._discard_next = True
        with self._data_lock:
            self._available = True
            self._reason = None
        print("CLIMATE: DHT11 on, reading every %.0f s" % config.CLIMATE_READ_PERIOD_S)
        return True

    def _read(self, now):
        """One reading. io_lock held, sensor open."""
        try:
            temp_c, humidity_pct = self._sensor.read()
        except ValueError as err:  # a bad reading (checksum, no answer): normal now and then
            if self._discard_next:
                self._discard_next = False
                return
            with self._data_lock:
                self._failed += 1
                self._last_error = str(err)
            return
        except Exception as err:  # pigpiod gone (socket error): close, try again later
            self._close_hardware()
            self._unavailable(now, "lost pigpiod: %s" % err)
            return
        if self._discard_next:  # first reading after power-on: thrown away, even if it looks fine
            self._discard_next = False
            return
        with self._data_lock:
            self._reading = (temp_c, humidity_pct, now)
            self._ok += 1

    def _unavailable(self, now, reason):
        with self._data_lock:
            changed = reason != self._reason
            self._available = False
            self._reason = reason
        self._retry_at = now + config.CLIMATE_RETRY_S
        if changed:
            print("CLIMATE: %s (retry every %.0f s)" % (reason, config.CLIMATE_RETRY_S))

    def _close_hardware(self):
        """Sensor off (power pin LOW), then disconnect. io_lock held. Never raises."""
        sensor, pi = self._sensor, self._pi
        self._sensor = self._pi = None
        if sensor is not None:
            try:
                sensor.close()
            except Exception:
                traceback.print_exc()
        self._stop_pi(pi)

    @staticmethod
    def _stop_pi(pi):
        if pi is None:
            return
        try:
            pi.stop()
        except Exception:
            traceback.print_exc()

    # ---------- thread ----------

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self.tick(self._clock())
            except Exception:
                traceback.print_exc()  # this thread must never die
            self._stop_event.wait(config.CLIMATE_READ_PERIOD_S)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="climate")
        self._thread.daemon = True
        self._thread.start()

    def close(self):
        """Stop the thread, switch the sensor off, disconnect. Safe to call twice."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(config.DHT11_POWER_ON_WAIT_S + 1.0)
        with self._io_lock:  # waits for a read / power-on that is still running
            if self._closed:
                return
            self._closed = True
            self._close_hardware()
        with self._data_lock:
            self._available = False
            self._reason = "robot server stopped"

    # ---------- status ----------

    def snapshot(self, now):
        with self._data_lock:
            return _snapshot(self._available, self._reading, now, self._ok, self._failed,
                             self._last_error, self._reason)
