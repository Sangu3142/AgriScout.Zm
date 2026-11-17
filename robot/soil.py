"""SoilMonitor: reads the soil moisture probe in the background, through an Arduino Uno on USB.

Runs inside the robot server (Python 3.7). The Uno is on USB serial only, never on the GPIO, so this
file has nothing to do with hardware.py. The Uno sends "READY" when it starts and then
"SOIL <raw 0-1023>" every 2 s. We only read: nothing is ever sent to the Uno.

Like climate.py, the server hands the monitor two functions, so the tests can pass fakes (the Windows
.venv has no pyserial):
  open_port(path)   -> an object with readline() (bytes, b"" on timeout) and close()   open_serial
  find_ports()      -> list of device paths to try, best first                         find_ports

tick(now) does one step and never sleeps (a readline waits at most config.SOIL_READ_TIMEOUT_S), so
the tests run on fake time.

Rules:
- Uno missing or unplugged: "Uno not connected", never crash, look again every config.SOIL_RETRY_S.
- Opening the port resets the Uno: the first line comes ~2 s later ("waiting for the Uno").
- Lines that are not "SOIL <0-1023>" ("READY", noise after a reset) are ignored.
- No line for config.SOIL_STALE_S: stale, shown as not available (the last values are kept).
- Soil is information only: it never changes the scan status, the LED or the driving.
"""

import glob
import os
import threading
import time
import traceback

import config

NOT_CONNECTED = "Uno not connected"
WAITING = "waiting for the Uno (it restarts when the port opens)"


def parse_line(text):
    """'SOIL 734' -> 734. Anything else (READY, noise, out of range) -> None."""
    parts = text.split()
    if len(parts) != 2 or parts[0] != "SOIL":
        return None
    try:
        raw = int(parts[1])
    except ValueError:
        return None
    if raw < 0 or raw > 1023:
        return None
    return raw


def moisture_pct(raw):
    """0 % at config.SOIL_RAW_DRY, 100 % at config.SOIL_RAW_WET, straight line in between, clamped."""
    pct = (config.SOIL_RAW_DRY - raw) * 100.0 / (config.SOIL_RAW_DRY - config.SOIL_RAW_WET)
    return int(round(min(100.0, max(0.0, pct))))


def soil_state(raw):
    """'dry', 'wet' or 'ok' from the raw value (higher raw = drier)."""
    if raw > config.SOIL_DRY_ABOVE:
        return "dry"
    if raw < config.SOIL_WET_BELOW:
        return "wet"
    return "ok"


def open_serial(path):
    """The real port. pyserial is imported only here: it's on the Pi, not in the Windows .venv."""
    import serial
    return serial.Serial(path, config.SOIL_BAUD, timeout=config.SOIL_READ_TIMEOUT_S)


def find_ports(exists=os.path.exists, list_glob=glob.glob):
    """config.SOIL_PORT if it is there, else any USB serial device (CH340 = ttyUSB, others = ttyACM)."""
    if exists(config.SOIL_PORT):
        return [config.SOIL_PORT]
    return sorted(list_glob("/dev/ttyUSB*")) + sorted(list_glob("/dev/ttyACM*"))


def _snapshot(reading, now, reason):
    """The /api/status "soil" dict. reading = (raw, read_at) or None."""
    if reading is None:
        return {"available": False, "raw": None, "moisture_pct": None, "state": None,
                "age_s": None, "reason": reason}
    raw, read_at = reading
    age_s = round(max(0.0, now - read_at), 1)
    available = reason is None and age_s <= config.SOIL_STALE_S
    if reason is None and not available:
        reason = "no reading for %d s" % int(age_s)
    return {"available": available, "raw": raw, "moisture_pct": moisture_pct(raw),
            "state": soil_state(raw), "age_s": age_s, "reason": None if available else reason}


class DisabledSoil(object):
    """Stands in for the monitor when config.SOIL_ENABLED is False (or in tests)."""

    REASON = "soil monitor turned off (config.SOIL_ENABLED = False)"

    def start(self):
        pass

    def close(self):
        pass

    def snapshot(self, now):
        return _snapshot(None, now, self.REASON)


class SoilMonitor(object):

    def __init__(self, open_port, find_ports, clock=time.monotonic):
        self._open_port = open_port
        self._find_ports = find_ports
        self._clock = clock
        # _io_lock: around open/readline/close. _data_lock: only around the numbers, so
        # /api/status never waits for a readline.
        self._io_lock = threading.Lock()
        self._data_lock = threading.Lock()
        self._port = None
        self._path = None
        self._retry_at = None  # None = look for the Uno at the next tick
        self._closed = False
        self._stop_event = threading.Event()
        self._thread = None

        self._reason = "starting"
        self._reading = None   # (raw, read_at) of the last good line
        self.bad_lines = 0     # ignored lines (READY, noise): for tests and debugging

    # ---------- state machine ----------

    def tick(self, now):
        with self._io_lock:
            if self._closed:
                return
            if self._port is None:
                if self._retry_at is not None and now < self._retry_at:
                    return
                self._open(now)
                return
            self._read(now)

    @property
    def connected(self):
        return self._port is not None

    def _open(self, now):
        """Find the Uno and open its port. io_lock held."""
        error = None
        try:
            paths = list(self._find_ports())
        except Exception as err:
            paths, error = [], "cannot list serial ports: %s" % err
        for path in paths:
            try:
                port = self._open_port(path)
            except Exception as err:  # e.g. permission denied (pi not in dialout), busy
                error = "cannot open %s: %s" % (path, err)
                continue
            self._port, self._path = port, path
            self._set_reason(WAITING)
            print("SOIL: Uno on %s, waiting for its first line" % path)
            return
        self._unavailable(now, error or NOT_CONNECTED)

    def _read(self, now):
        """One line. io_lock held, port open."""
        try:
            line = self._port.readline()
        except Exception as err:  # unplugged: SerialException / OSError
            print("SOIL: lost the Uno on %s (%s)" % (self._path, err))
            self._close_port()
            self._unavailable(now, NOT_CONNECTED)
            return
        if not line:
            return  # timeout: nothing this second (the Uno sends every 2 s)
        raw = parse_line(line.decode("ascii", "replace"))
        if raw is None:
            self.bad_lines += 1
            return
        with self._data_lock:
            first = self._reading is None
            self._reading = (raw, now)
            self._reason = None
        if first:
            print("SOIL: first reading %d (%s)" % (raw, soil_state(raw)))

    def _set_reason(self, reason):
        with self._data_lock:
            self._reason = reason

    def _unavailable(self, now, reason):
        with self._data_lock:
            self._reading = None
            changed = reason != self._reason
            self._reason = reason
        self._retry_at = now + config.SOIL_RETRY_S
        if changed:
            print("SOIL: %s (retry every %.0f s)" % (reason, config.SOIL_RETRY_S))

    def _close_port(self):
        """io_lock held. Never raises."""
        port = self._port
        self._port = self._path = None
        if port is None:
            return
        try:
            port.close()
        except Exception:
            traceback.print_exc()

    # ---------- thread ----------

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self.tick(self._clock())
            except Exception:
                traceback.print_exc()  # this thread must never die
            # Connected: readline already waits up to SOIL_READ_TIMEOUT_S. Not connected: the
            # retry time decides when to look again; check it once a second.
            self._stop_event.wait(0 if self.connected else 1.0)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="soil")
        self._thread.daemon = True
        self._thread.start()

    def close(self):
        """Stop the thread and close the port. Safe to call twice."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(config.SOIL_READ_TIMEOUT_S + 1.0)
        with self._io_lock:  # waits for a readline that is still running
            if self._closed:
                return
            self._closed = True
            self._close_port()
        with self._data_lock:
            self._reading = None
            self._reason = "robot server stopped"

    # ---------- status ----------

    def snapshot(self, now):
        with self._data_lock:
            return _snapshot(self._reading, now, self._reason)
