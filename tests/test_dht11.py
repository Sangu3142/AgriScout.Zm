"""DHT11: the decoder (shared.decode_dht11), the pigpio reader (hardware.Dht11), connect_pigpio()
and the servo flag (config.SERVOS_USE_PIGPIO).

Windows has neither RPi.GPIO nor pigpio, so hardware.py is imported with small stand-ins.
"""

import argparse
import ast
import glob
import importlib.util
import os
import sys
import types

import pytest

import config
import shared
from conftest import ROBOT_DIR


def edges_for(data, start=1000, one_us=70, zero_us=27):
    """The (level, tick) edges pigpio reports for a DHT11 answer carrying these 5 bytes."""
    t = start
    edges = [(0, t)]               # our start signal: DATA low...
    t += 20000
    edges.append((1, t))           # ...released: the pull-up takes it high
    t += 30
    edges.append((0, t))           # the sensor answers: 80 us low,
    t += 80
    edges.append((1, t))           # 80 us high ("ready")
    t += 80
    for byte in data:
        for bit in range(7, -1, -1):
            edges.append((0, t))   # each bit: 50 us low...
            t += 50
            edges.append((1, t))   # ...then high: ~27 us = 0, ~70 us = 1
            t += one_us if byte >> bit & 1 else zero_us
    edges.append((0, t))           # end: 50 us low, then released (a rise with no fall after it)
    t += 50
    edges.append((1, t))
    return [(level, tick & 0xFFFFFFFF) for level, tick in edges]  # ticks are 32 bit


def with_checksum(*values):
    return list(values) + [sum(values) & 0xFF]


# ---------------------------------------------------------------- decoder

def test_decodes_a_reading():
    assert shared.decode_dht11(edges_for(with_checksum(58, 0, 23, 0))) == (23.0, 58.0)


def test_tenths_and_below_zero():
    # newer DHT11s: tenths in bytes 1 and 3; top bit of byte 3 = below 0 C
    assert shared.decode_dht11(edges_for(with_checksum(45, 3, 2, 0x85))) == (-2.5, 45.3)


def test_bit_threshold_is_50_us():
    edges = edges_for(with_checksum(58, 0, 23, 0), one_us=55, zero_us=45)
    assert shared.decode_dht11(edges) == (23.0, 58.0)


def test_tick_wraparound():
    # pigpio's tick wraps from 2**32 - 1 to 0 every ~72 minutes: here, in the middle of the answer
    edges = edges_for(with_checksum(58, 0, 23, 0), start=2 ** 32 - 21000)
    assert edges[0][1] > edges[-1][1]
    assert shared.decode_dht11(edges) == (23.0, 58.0)


def test_only_the_last_40_pulses_count():
    stale = [(1, 10), (0, 40), (1, 60), (0, 200)]  # e.g. late edges from an earlier reading
    assert shared.decode_dht11(stale + edges_for(with_checksum(58, 0, 23, 0))) == (23.0, 58.0)


def test_checksum_error():
    with pytest.raises(ValueError, match="checksum"):
        shared.decode_dht11(edges_for([58, 0, 23, 0, 80]))


@pytest.mark.parametrize("edges, got", [
    ([], 0),
    (edges_for(with_checksum(58, 0, 23, 0))[:30], 14),  # release + ready + 12 bits, then cut off
])
def test_no_answer_says_check_wiring(edges, got):
    with pytest.raises(ValueError, match=r"no answer \(%d of 40 bits\): check wiring" % got):
        shared.decode_dht11(edges)


def test_all_zeros_is_not_a_reading():
    with pytest.raises(ValueError, match="all zeros"):
        shared.decode_dht11(edges_for([0, 0, 0, 0, 0]))


@pytest.mark.parametrize("data", [with_checksum(120, 0, 23, 0),   # humidity above 100 %
                                  with_checksum(0, 0, 23, 0),     # humidity 0
                                  with_checksum(50, 0, 70, 0)])   # 70 C
def test_out_of_range(data):
    with pytest.raises(ValueError, match="out of range"):
        shared.decode_dht11(edges_for(data))


# ---------------------------------------------------------------- hardware.py with stand-ins

class FakePWM(object):
    def __init__(self, pin, hz):
        self.pin = pin

    def start(self, duty):
        pass

    def ChangeDutyCycle(self, duty):
        pass

    def stop(self):
        pass


class FakePi(object):
    """pigpio.pi stand-in: records calls. When DATA is released after the start signal,
    "the sensor" answers: the callback gets self.answer."""

    def __init__(self, answer=(), connected=True):
        self.answer = answer
        self.connected = connected
        self.calls = []
        self.on_edge = None

    def set_mode(self, gpio, mode):
        self.calls.append(("set_mode", gpio, mode))
        if mode == "INPUT" and self.calls[-2:-1] == [("write", gpio, 0)]:
            for level, tick in self.answer:
                self.on_edge(gpio, level, tick)

    def set_pull_up_down(self, gpio, pud):
        self.calls.append(("pull", gpio, pud))

    def write(self, gpio, level):
        self.calls.append(("write", gpio, level))

    def callback(self, gpio, edge, func):
        self.calls.append(("callback", gpio, edge))
        self.on_edge = func
        return types.SimpleNamespace(cancel=lambda: self.calls.append(("cancel", gpio)))


@pytest.fixture
def hardware(monkeypatch):
    """robot/hardware.py imported with stand-ins for RPi.GPIO and pigpio."""
    gpio = types.ModuleType("RPi.GPIO")
    gpio.BCM, gpio.OUT, gpio.IN, gpio.LOW, gpio.HIGH = "BCM", "OUT", "IN", 0, 1
    for name in ("setwarnings", "setmode", "setup", "output", "cleanup"):
        setattr(gpio, name, lambda *args, **kwargs: None)
    gpio.input = lambda pin: 0
    gpio.PWM = FakePWM
    rpi = types.ModuleType("RPi")
    rpi.GPIO = gpio
    pigpio = types.ModuleType("pigpio")
    pigpio.INPUT, pigpio.OUTPUT = "INPUT", "OUTPUT"
    pigpio.PUD_OFF, pigpio.EITHER_EDGE = "PUD_OFF", "EITHER_EDGE"
    pigpio.made = []                     # every pigpio.pi(...) connection: (host, port)

    def make_pi(host, port):
        pigpio.made.append((host, port))
        return FakePi(connected=pigpio.reachable)
    pigpio.pi = make_pi
    pigpio.reachable = True
    monkeypatch.setitem(sys.modules, "RPi", rpi)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", gpio)
    monkeypatch.setitem(sys.modules, "pigpio", pigpio)
    monkeypatch.delitem(sys.modules, "hardware", raising=False)
    import hardware
    yield hardware
    sys.modules.pop("hardware", None)  # the next import gets the real modules again (on the Pi)


def test_dht11_read_sends_the_start_signal_then_decodes(hardware, monkeypatch):
    monkeypatch.setattr(config, "DHT11_START_LOW_S", 0)
    monkeypatch.setattr(config, "DHT11_ANSWER_WAIT_S", 0)
    pi = FakePi(edges_for(with_checksum(58, 0, 23, 0)))
    sensor = hardware.Dht11(pi, 17, power_pin=None)                          # fixed 3.3 V pin
    assert pi.calls == [("set_mode", 17, "INPUT"), ("pull", 17, "PUD_OFF"),  # module's pull-up
                        ("callback", 17, "EITHER_EDGE")]
    assert sensor.read() == (23.0, 58.0)
    assert pi.calls[3:] == [("set_mode", 17, "OUTPUT"), ("write", 17, 0),    # start: DATA low,
                            ("set_mode", 17, "INPUT")]                       # then released
    pi.answer = edges_for(with_checksum(58, 0, 23, 0))[:20]                  # a broken answer:
    with pytest.raises(ValueError, match="no answer"):                       # the old edges
        sensor.read()                                                        # were cleared
    sensor.close()
    assert pi.calls[-2:] == [("cancel", 17), ("set_mode", 17, "INPUT")]


def test_dht11_default_pin_is_17(hardware, monkeypatch):
    monkeypatch.setattr(config, "DHT11_POWER_ON_WAIT_S", 0)
    assert config.DHT11_PIN == 17
    pi = FakePi()
    hardware.Dht11(pi)
    assert pi.calls[0] == ("set_mode", 17, "INPUT")


def test_dht11_power_pin_on_when_opened_off_when_closed(hardware, monkeypatch):
    monkeypatch.setattr(config, "DHT11_START_LOW_S", 0)
    monkeypatch.setattr(config, "DHT11_ANSWER_WAIT_S", 0)
    slept = []
    monkeypatch.setattr(hardware.time, "sleep", slept.append)
    pi = FakePi(edges_for(with_checksum(58, 0, 23, 0)))
    sensor = hardware.Dht11(pi, 17, power_pin=12)
    assert pi.calls[3:] == [("set_mode", 12, "OUTPUT"), ("write", 12, 1)]   # 3.3 V on VCC
    assert slept == [2.0]                    # power-on wait (> 1 s) before the first read
    assert sensor.read() == (23.0, 58.0)
    sensor.close()
    assert pi.calls[-4:] == [("cancel", 17), ("set_mode", 17, "INPUT"),
                             ("write", 12, 0), ("set_mode", 12, "INPUT")]   # off, then input


def test_dht11_without_power_pin_never_touches_12(hardware, monkeypatch):
    slept = []
    monkeypatch.setattr(hardware.time, "sleep", slept.append)
    pi = FakePi()
    hardware.Dht11(pi, 17, power_pin=None).close()
    assert all(call[1] != 12 for call in pi.calls)
    assert slept == []                       # no power-on wait either


def test_dht11_default_power_pin_is_12(hardware, monkeypatch):
    monkeypatch.setattr(config, "DHT11_POWER_ON_WAIT_S", 0)
    assert config.DHT11_POWER_PIN == 12
    pi = FakePi()
    hardware.Dht11(pi)
    assert ("write", 12, 1) in pi.calls


def test_bcm_12_is_only_the_dht11_power_pin(hardware):
    # Every pin of the robot is a config.py constant: none other may be 12.
    twelves = {name for name in dir(config)
               if name.isupper() and getattr(config, name) == 12}
    assert twelves == {"DHT11_POWER_PIN"}
    assert 12 not in hardware.SERVO_PINS
    assert 12 not in [pin for _, pin in hardware.LINE_PINS]


def test_dht_test_power_pin_option(hardware):
    spec = importlib.util.spec_from_file_location(
        "dht_test", os.path.join(ROBOT_DIR, "tools", "dht_test.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool._power_pin("none") is None and tool._power_pin("None") is None
    assert tool._power_pin("12") == 12
    with pytest.raises(argparse.ArgumentTypeError):
        tool._power_pin("twelve")


def test_connect_pigpio(hardware, monkeypatch):
    monkeypatch.setattr(hardware, "_pigpiod_running", lambda: False)
    assert hardware.connect_pigpio() == (None, "pigpiod is not running")
    assert sys.modules["pigpio"].made == []        # no daemon: never connect (8888 = Jupyter)

    monkeypatch.setattr(hardware, "_pigpiod_running", lambda: True)
    pi, reason = hardware.connect_pigpio()
    assert pi.connected and reason is None
    assert sys.modules["pigpio"].made == [("localhost", 8889)]

    sys.modules["pigpio"].reachable = False
    assert hardware.connect_pigpio() == (None, "pigpiod not reachable on port 8889")


def test_servos_ignore_pigpiod_by_default(hardware, monkeypatch):
    asked = []
    monkeypatch.setattr(hardware, "connect_pigpio", lambda: asked.append(1) or (FakePi(), None))
    assert config.SERVOS_USE_PIGPIO is False
    robot = hardware.RealRobot()
    assert asked == [] and robot._pi is None       # never even asks for pigpiod
    assert set(robot._pwm_servos) == set(hardware.SERVO_PINS)  # RPi.GPIO PWM, as tested on the tank
    robot.cleanup()


def test_servos_use_pigpio_only_when_asked(hardware, monkeypatch, capsys):
    monkeypatch.setattr(config, "SERVOS_USE_PIGPIO", True)
    monkeypatch.setattr(hardware, "connect_pigpio", lambda: (None, "pigpiod is not running"))
    robot = hardware.RealRobot()
    assert robot._pi is None and set(robot._pwm_servos) == set(hardware.SERVO_PINS)
    assert "WARNING: pigpiod is not running, servos use RPi.GPIO PWM" in capsys.readouterr().out

    monkeypatch.setattr(hardware, "connect_pigpio", lambda: (FakePi(), None))
    robot = hardware.RealRobot()
    assert isinstance(robot._pi, FakePi) and robot._pwm_servos == {}


# ---------------------------------------------------------------- rules

def test_only_hardware_py_imports_gpio_libraries():
    # CLAUDE.md: hardware.py is the only file that imports RPi.GPIO / pigpio (tools/ included).
    for path in glob.glob(os.path.join(ROBOT_DIR, "**", "*.py"), recursive=True):
        if os.path.basename(path) == "hardware.py":
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = {alias.name.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for alias in node.names}
        names |= {node.module.split(".")[0] for node in ast.walk(tree)
                  if isinstance(node, ast.ImportFrom) and node.module}
        assert not names & {"RPi", "pigpio"}, path


def test_dht_test_tool_exists_and_uses_hardware_py():
    path = os.path.join(ROBOT_DIR, "tools", "dht_test.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "hardware.connect_pigpio()" in src and "hardware.Dht11(" in src
    assert "sudo pigpiod -l -p" in src and "PERIOD_S = 2.0" in src
    assert "--power-pin" in src and "power_pin=args.power_pin" in src
