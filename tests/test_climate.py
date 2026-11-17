"""climate.ClimateMonitor with a scripted fake DHT11 and fake pigpio connections (fake time)."""

import climate
import config
from fake_hardware import FAKE_CLIMATE, FakeDht11, FakePigpio, fake_connect_pigpio


class ScriptedSensor(object):
    """Each read() returns / raises the next item of `script`."""

    def __init__(self, script):
        self.script = list(script)
        self.reads = 0
        self.closed = False

    def read(self):
        self.reads += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class Hardware(object):
    """connect() / open_sensor() for the monitor. `daemon` False = pigpiod not running."""

    def __init__(self, script, daemon=True):
        self.script = script
        self.daemon = daemon
        self.connects = 0
        self.pis = []
        self.sensors = []

    def connect(self):
        self.connects += 1
        if not self.daemon:
            return None, "pigpiod is not running"
        pi = FakePigpio()
        self.pis.append(pi)
        return pi, None

    def open_sensor(self, pi):
        sensor = ScriptedSensor(self.script)
        self.sensors.append(sensor)
        return sensor


def monitor_for(hw):
    return climate.ClimateMonitor(hw.connect, hw.open_sensor)


def test_first_read_after_power_on_is_discarded():
    hw = Hardware([(99.0, 99.0), (24.5, 61.0)])
    mon = monitor_for(hw)
    mon.tick(0.0)
    s = mon.snapshot(0.0)
    assert s["available"] is True and s["temp_c"] is None and s["age_s"] is None
    assert s["ok"] == 0 and s["failed"] == 0           # thrown away, not counted
    mon.tick(5.0)
    s = mon.snapshot(6.0)
    assert (s["temp_c"], s["humidity_pct"], s["age_s"], s["ok"]) == (24.5, 61.0, 1.0, 1)


def test_failed_first_read_is_not_counted_either():
    hw = Hardware([ValueError("no answer"), (24.5, 61.0)])
    mon = monitor_for(hw)
    mon.tick(0.0)
    assert mon.snapshot(0.0)["failed"] == 0
    mon.tick(5.0)
    assert mon.snapshot(5.0)["ok"] == 1


def test_failure_keeps_last_good_reading_and_its_age_grows():
    hw = Hardware([(1.0, 1.0), (24.5, 61.0), ValueError("checksum error"), (25.0, 62.0)])
    mon = monitor_for(hw)
    mon.tick(0.0)
    mon.tick(5.0)
    mon.tick(10.0)
    s = mon.snapshot(12.0)
    assert (s["temp_c"], s["humidity_pct"], s["age_s"]) == (24.5, 61.0, 7.0)
    assert (s["ok"], s["failed"], s["last_error"]) == (1, 1, "checksum error")
    assert s["available"] is True and s["reason"] is None
    mon.tick(15.0)
    s = mon.snapshot(15.0)
    assert (s["temp_c"], s["age_s"], s["ok"], s["failed"]) == (25.0, 0.0, 2, 1)


def test_pigpiod_missing_never_crashes_and_retries_every_30_s():
    hw = Hardware([(1.0, 1.0), (24.5, 61.0)], daemon=False)
    mon = monitor_for(hw)
    mon.tick(0.0)
    s = mon.snapshot(0.0)
    assert s["available"] is False and s["temp_c"] is None
    assert s["reason"] == "pigpiod not running - start it with: sudo pigpiod -l -p 8889"
    for now in (5.0, 10.0, 29.9):
        mon.tick(now)
    assert hw.connects == 1                    # no retry before 30 s
    hw.daemon = True                           # user starts pigpiod
    mon.tick(30.0)
    assert hw.connects == 2 and mon.snapshot(30.0)["available"] is True
    mon.tick(35.0)
    assert mon.snapshot(35.0)["temp_c"] == 24.5


def test_other_connect_reason_is_shown():
    mon = climate.ClimateMonitor(lambda: (None, "pigpiod not reachable on port 8889"),
                                 lambda pi: None)
    mon.tick(0.0)
    assert mon.snapshot(0.0)["reason"] == ("pigpiod not reachable on port 8889 "
                                           "(start pigpiod with: sudo pigpiod -l -p 8889)")


def test_pigpiod_lost_while_running():
    hw = Hardware([(1.0, 1.0), (24.5, 61.0), ConnectionResetError("reset by peer")])
    mon = monitor_for(hw)
    mon.tick(0.0)
    mon.tick(5.0)
    mon.tick(10.0)                             # pigpiod killed: socket error, not ValueError
    s = mon.snapshot(11.0)
    assert s["available"] is False and s["reason"] == "lost pigpiod: reset by peer"
    assert s["temp_c"] == 24.5 and s["age_s"] == 6.0    # last good reading still shown, aging
    assert hw.sensors[0].closed and hw.pis[0].stopped
    hw.script[:] = [(1.0, 1.0), (26.0, 50.0)]
    mon.tick(15.0)
    assert hw.connects == 1                    # waits 30 s before reconnecting
    mon.tick(40.0)
    mon.tick(45.0)                             # power-on read discarded again, then a good one
    assert hw.connects == 2 and mon.snapshot(45.0)["temp_c"] == 26.0


def test_sensor_that_fails_to_open():
    def broken(pi):
        raise OSError("bad gpio")
    hw = Hardware([])
    mon = climate.ClimateMonitor(hw.connect, broken)
    mon.tick(0.0)
    s = mon.snapshot(0.0)
    assert s["available"] is False and s["reason"] == "cannot open the DHT11: bad gpio"
    assert hw.pis[0].stopped


def test_close_switches_the_sensor_off_and_is_safe_twice():
    hw = Hardware([(1.0, 1.0)])
    mon = monitor_for(hw)
    mon.tick(0.0)
    mon.close()
    mon.close()
    assert hw.sensors[0].closed and hw.pis[0].stopped
    assert mon.snapshot(1.0)["available"] is False
    mon.tick(100.0)                            # after close: never opens again
    assert hw.connects == 1


def test_snapshot_shape():
    s = climate.ClimateMonitor(fake_connect_pigpio, FakeDht11).snapshot(0.0)
    assert set(s) == {"available", "temp_c", "humidity_pct", "age_s", "ok", "failed",
                      "last_error", "reason"}


def test_fake_hardware_climate_is_deterministic():
    mon = climate.ClimateMonitor(fake_connect_pigpio, FakeDht11)
    mon.tick(0.0)
    mon.tick(5.0)
    s = mon.snapshot(5.0)
    assert (s["temp_c"], s["humidity_pct"]) == FAKE_CLIMATE == (24.0, 60.0)


def test_disabled_climate():
    s = climate.DisabledClimate().snapshot(0.0)
    assert s["available"] is False and s["reason"] == climate.DisabledClimate.REASON
    assert "CLIMATE_ENABLED" in s["reason"]


def test_thread_reads_in_the_background(monkeypatch):
    monkeypatch.setattr(config, "CLIMATE_READ_PERIOD_S", 0.01)
    hw = Hardware([(1.0, 1.0)] + [(24.5, 61.0)] * 1000)
    mon = monitor_for(hw)
    mon.start()
    try:
        for _ in range(200):
            if mon.snapshot(0.0)["ok"] >= 2:
                break
            mon._stop_event.wait(0.01)
    finally:
        mon.close()
    assert mon.snapshot(0.0)["temp_c"] == 24.5
    assert hw.sensors[0].closed


def test_config_values():
    assert config.CLIMATE_ENABLED is True
    assert config.CLIMATE_READ_PERIOD_S == 5.0 and config.CLIMATE_RETRY_S == 30.0
