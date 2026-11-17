"""soil.SoilMonitor with scripted fake serial ports (fake time, no real serial, no pyserial)."""

import pytest

import config
import soil
from fake_hardware import FAKE_SOIL_RAW, FakeSoil, fake_find_soil_ports


# ---------------------------------------------------------------- pure helpers

@pytest.mark.parametrize("text, raw", [
    ("SOIL 734", 734), ("SOIL 734\r\n", 734), ("  SOIL   0 \n", 0), ("SOIL 1023", 1023),
    ("READY", None), ("READY\r\n", None), ("", None), ("SOIL", None), ("SOIL abc", None),
    ("SOIL 1024", None), ("SOIL -1", None), ("SOIL 7 8", None), ("soil 500", None),
    ("��SOIL 500", None),   # noise after the Uno resets
])
def test_parse_line(text, raw):
    assert soil.parse_line(text) == raw


@pytest.mark.parametrize("text", ["PUMP ON", "PUMP OFF", "PUMP LOCKOUT"])
def test_parse_line_ignores_pump_lines(text):
    # the Uno's watering messages are not soil readings: the robot ignores them
    assert soil.parse_line(text) is None


@pytest.mark.parametrize("raw, pct", [
    (1000, 0), (300, 100), (650, 50), (700, 43), (600, 57),
    (1023, 0), (0, 100),              # beyond the calibration: clamped
])
def test_moisture_pct(raw, pct):
    assert soil.moisture_pct(raw) == pct


@pytest.mark.parametrize("raw, state", [
    (851, "dry"), (850, "ok"), (700, "ok"), (500, "ok"), (499, "wet"), (1023, "dry"), (300, "wet"),
])
def test_soil_state(raw, state):
    assert soil.soil_state(raw) == state


def test_calibration_from_config():
    assert (config.SOIL_PORT, config.SOIL_BAUD) == ("/dev/ttyUSB0", 9600)
    assert (config.SOIL_RAW_DRY, config.SOIL_RAW_WET) == (1000, 300)
    assert (config.SOIL_DRY_ABOVE, config.SOIL_WET_BELOW) == (850, 500)
    assert config.SOIL_RETRY_S == 10.0 and config.SOIL_STALE_S == 10.0
    assert config.SOIL_ENABLED is True


def test_find_ports_prefers_soil_port():
    assert soil.find_ports(exists=lambda p: True, list_glob=lambda g: ["/dev/ttyUSB3"]) == \
        ["/dev/ttyUSB0"]


def test_find_ports_falls_back_to_usb_then_acm():
    found = {"/dev/ttyUSB*": ["/dev/ttyUSB2", "/dev/ttyUSB1"], "/dev/ttyACM*": ["/dev/ttyACM0"]}
    ports = soil.find_ports(exists=lambda p: False, list_glob=lambda g: found[g])
    assert ports == ["/dev/ttyUSB1", "/dev/ttyUSB2", "/dev/ttyACM0"]


def test_find_ports_nothing_plugged_in():
    assert soil.find_ports(exists=lambda p: False, list_glob=lambda g: []) == []


# ---------------------------------------------------------------- monitor

class ScriptedPort(object):
    """Each readline() returns / raises the next item of `script` (b"" = timeout when empty)."""

    def __init__(self, path, script):
        self.path = path
        self.script = script
        self.closed = False
        self.written = []

    def readline(self):
        if not self.script:
            return b""
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def write(self, data):  # must never be called: the Uno is read only
        self.written.append(data)

    def close(self):
        self.closed = True


class Usb(object):
    """find_ports() / open_port() for the monitor. `paths` = what is plugged in right now."""

    def __init__(self, script, paths=("/dev/ttyUSB0",), fail=()):
        self.script = list(script)
        self.paths = list(paths)
        self.fail = dict(fail)   # path -> exception raised by open
        self.finds = 0
        self.ports = []

    def find_ports(self):
        self.finds += 1
        return list(self.paths)

    def open_port(self, path):
        if path in self.fail:
            raise self.fail[path]
        port = ScriptedPort(path, self.script)
        self.ports.append(port)
        return port


def monitor_for(usb):
    return soil.SoilMonitor(usb.open_port, usb.find_ports)


def test_ready_then_soil_line_gives_a_reading():
    usb = Usb([b"READY\r\n", b"SOIL 734\r\n"])
    mon = monitor_for(usb)
    mon.tick(0.0)                                   # opens the port (the Uno resets)
    s = mon.snapshot(0.0)
    assert s["available"] is False and s["raw"] is None and "waiting for the Uno" in s["reason"]
    mon.tick(1.0)                                   # READY: ignored
    assert mon.snapshot(1.0)["raw"] is None
    mon.tick(2.0)
    s = mon.snapshot(2.5)
    assert s == {"available": True, "raw": 734, "moisture_pct": 38, "state": "ok",
                 "age_s": 0.5, "reason": None}
    assert usb.ports[0].written == []               # nothing ever sent to the Uno


def test_timeout_and_malformed_lines_are_ignored():
    usb = Usb([b"SOIL 600\r\n", b"", b"garbage\r\n", b"SOIL 99999\r\n", b"\xff\xfe\r\n"])
    mon = monitor_for(usb)
    mon.tick(0.0)
    for t in (1.0, 2.0, 3.0, 4.0, 5.0):
        mon.tick(t)
    s = mon.snapshot(5.0)
    assert s["available"] is True and s["raw"] == 600 and s["age_s"] == 4.0   # kept
    assert mon.bad_lines == 3                       # the b"" timeout is not a bad line


def test_reading_goes_stale_after_10_s():
    usb = Usb([b"SOIL 900\r\n"])
    mon = monitor_for(usb)
    mon.tick(0.0)
    mon.tick(1.0)
    assert mon.snapshot(11.0)["available"] is True               # exactly 10 s: still fresh
    s = mon.snapshot(13.4)
    assert s["available"] is False and s["reason"] == "no reading for 12 s"
    assert (s["raw"], s["moisture_pct"], s["state"]) == (900, 14, "dry")   # last values kept


def test_no_uno_plugged_in_retries_every_10_s():
    usb = Usb([], paths=())
    mon = monitor_for(usb)
    mon.tick(0.0)
    s = mon.snapshot(0.0)
    assert s["available"] is False and s["reason"] == "Uno not connected" and s["raw"] is None
    mon.tick(5.0)
    assert usb.finds == 1                            # not before the retry time
    usb.paths = ["/dev/ttyUSB0"]
    usb.script.append(b"SOIL 450\r\n")
    mon.tick(10.0)                                   # plugged in now: opened
    assert usb.finds == 2 and len(usb.ports) == 1
    mon.tick(11.0)
    assert mon.snapshot(11.0)["state"] == "wet"


def test_unplugged_while_reading():
    usb = Usb([b"SOIL 600\r\n", OSError(5, "Input/output error")])
    mon = monitor_for(usb)
    mon.tick(0.0)
    mon.tick(1.0)
    mon.tick(2.0)                                    # readline raises: Uno gone
    assert usb.ports[0].closed is True
    s = mon.snapshot(2.0)
    assert s["available"] is False and s["reason"] == "Uno not connected"
    assert s["raw"] is None                           # old reading cleared
    usb.paths = []
    mon.tick(5.0)
    assert usb.finds == 1                             # waits for the retry time
    usb.paths = ["/dev/ttyUSB0"]
    usb.script.append(b"SOIL 700\r\n")
    mon.tick(12.0)
    mon.tick(13.0)
    assert mon.snapshot(13.0)["raw"] == 700           # back again


def test_open_error_tries_the_next_port_then_reports_it():
    usb = Usb([b"SOIL 800\r\n"], paths=["/dev/ttyUSB0", "/dev/ttyACM0"],
              fail={"/dev/ttyUSB0": OSError(13, "Permission denied")})
    mon = monitor_for(usb)
    mon.tick(0.0)
    assert usb.ports[0].path == "/dev/ttyACM0"
    mon.tick(1.0)
    assert mon.snapshot(1.0)["raw"] == 800

    usb2 = Usb([], fail={"/dev/ttyUSB0": OSError(13, "Permission denied")})
    mon2 = monitor_for(usb2)
    mon2.tick(0.0)
    reason = mon2.snapshot(0.0)["reason"]
    assert reason.startswith("cannot open /dev/ttyUSB0:") and "Permission denied" in reason


def test_find_ports_raising_never_crashes():
    def broken():
        raise RuntimeError("no /dev")
    mon = soil.SoilMonitor(lambda path: None, broken)
    mon.tick(0.0)
    assert "cannot list serial ports" in mon.snapshot(0.0)["reason"]


def test_close_closes_the_port_and_is_safe_twice():
    usb = Usb([b"SOIL 600\r\n"])
    mon = monitor_for(usb)
    mon.tick(0.0)
    mon.tick(1.0)
    mon.close()
    mon.close()
    assert usb.ports[0].closed is True
    s = mon.snapshot(2.0)
    assert s["available"] is False and s["reason"] == "robot server stopped"
    mon.tick(3.0)                                    # after close: does nothing
    assert len(usb.ports) == 1


def test_thread_starts_and_stops_quickly():
    usb = Usb([], paths=())
    mon = monitor_for(usb)
    mon.start()
    mon.close()
    assert not mon._thread.is_alive()
    assert mon.snapshot(0.0)["reason"] == "robot server stopped"


def test_disabled_soil():
    d = soil.DisabledSoil()
    d.start()
    s = d.snapshot(1.0)
    assert s["available"] is False and "SOIL_ENABLED" in s["reason"]
    assert set(s) == {"available", "raw", "moisture_pct", "state", "age_s", "reason"}
    d.close()


def test_fake_soil_like_the_firmware():
    port = FakeSoil("FAKE", sleep=lambda s: None)
    assert port.readline() == b"READY\r\n"
    assert port.readline() == b"SOIL 600\r\n"
    assert FAKE_SOIL_RAW == 600 and fake_find_soil_ports() == ["FAKE"]
    mon = soil.SoilMonitor(lambda path: FakeSoil(path, sleep=lambda s: None),
                           fake_find_soil_ports)
    for t in (0.0, 2.0, 4.0):
        mon.tick(t)
    s = mon.snapshot(4.0)
    assert (s["available"], s["raw"], s["moisture_pct"], s["state"]) == (True, 600, 57, "ok")


def test_soil_imports_no_gpio_or_serial_at_module_level():
    src = open(soil.__file__, encoding="utf-8").read()
    assert "RPi" not in src and "pigpio" not in src
    top = [line for line in src.splitlines() if line.startswith(("import ", "from "))]
    assert "import serial" not in top                # only inside open_serial (Pi only)
