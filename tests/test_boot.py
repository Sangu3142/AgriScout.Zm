"""Phase 4b B2: start at boot (--boot wait + guard for Yahboom's app) and the port guard."""

import os
import socket
import sys

import pytest

import config
import robot_server


class Clock(object):
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def appears_at(clock, at_s):
    """Fake pgrep: Yahboom's app is 'running' from at_s on."""
    return lambda: clock() >= at_s


def test_wait_finds_app_when_it_starts():
    clock = Clock()
    assert robot_server.wait_for_bluetooth_app(appears_at(clock, 3.0), clock, clock.sleep) is True
    assert 3.0 <= clock() < 3.0 + config.BOOT_POLL_S + 0.01   # stopped waiting right away


def test_wait_gives_up_after_timeout():
    clock = Clock()
    found = robot_server.wait_for_bluetooth_app(lambda: False, clock, clock.sleep, timeout_s=60)
    assert found is False
    assert 60 <= clock() < 61                                  # carried on after 60 s


def test_guard_kills_late_app_then_stops_guarding():
    clock = Clock()
    alive = {"yahboom": False}
    kills = []

    def running():
        if clock() >= 20 and not kills:
            alive["yahboom"] = True                            # desktop starts it late, once
        return alive["yahboom"]

    def stop():
        kills.append(clock())
        alive["yahboom"] = False

    n = robot_server.guard_bluetooth_app(running, stop, clock, clock.sleep, duration_s=180)
    assert n == 1 and 20 <= kills[0] < 20 + config.BOOT_GUARD_PERIOD_S
    assert clock() >= 180                                      # guarded for the whole time


def test_pgrep_command(monkeypatch):
    calls = []
    fake_call = lambda cmd, **kw: calls.append(cmd) or 0
    assert robot_server.bluetooth_app_running(fake_call) is True
    assert calls == [["pgrep", "-f", "bluetooth_control_tank"]]


@pytest.fixture
def busy_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)  # never accepts: each port_in_use() check leaves one pending connection
    yield s.getsockname()[1]
    s.close()


def test_port_in_use(busy_port):
    assert robot_server.port_in_use(busy_port) is True
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    assert robot_server.port_in_use(port) is False


def test_second_copy_exits_before_touching_gpio(busy_port, monkeypatch):
    """Port taken -> exit code 1, and neither the Yahboom pkill, nor hardware.py, nor any robot."""
    touched = []
    monkeypatch.setattr(robot_server, "stop_bluetooth_app", lambda: touched.append("pkill"))
    monkeypatch.setattr(robot_server, "FakeRobot", lambda: touched.append("robot"))
    monkeypatch.delitem(sys.modules, "hardware", raising=False)
    for argv in (["--port", str(busy_port)], ["--port", str(busy_port), "--boot"],
                 ["--fake", "--port", str(busy_port)]):
        assert robot_server.main(argv) == 1
    assert touched == []
    assert "hardware" not in sys.modules                       # RPi.GPIO never imported


def test_unit_file():
    path = os.path.join(os.path.dirname(robot_server.__file__), "agriscout.service")
    with open(path) as f:
        text = f.read()
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    assert "After=network.target rc-local.service" in lines
    assert "WantedBy=multi-user.target" in lines
    assert not any(l.startswith(("Wants=", "Requires=")) for l in lines)
    assert not any("graphical.target" in l for l in lines)     # would be an ordering cycle
    assert "User=pi" in lines and "Restart=on-failure" in lines
    assert "WorkingDirectory=/home/pi/agriscout/robot" in lines
    assert "Environment=PYTHONUNBUFFERED=1" in lines
    assert any(l.startswith("ExecStart=") and l.endswith("robot_server.py --boot") for l in lines)
