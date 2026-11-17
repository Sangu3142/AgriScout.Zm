"""ApiClient against the real Flask app + FakeRobot, over real HTTP on 127.0.0.1."""

import socket
import threading
import time

import pytest
from werkzeug.serving import make_server

import config
from api_client import ApiClient, normalize_address, parse_climate
from fake_hardware import FakeRobot
from robot_server import RobotController, create_app


def serve(app):
    """Run a WSGI app on a free port in a background thread. Returns (server, "127.0.0.1:port")."""
    server = make_server("127.0.0.1", 0, app, threaded=True)  # port 0 = OS picks a free one
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, "127.0.0.1:%d" % server.server_port


@pytest.fixture
def robot():
    return FakeRobot()


@pytest.fixture
def ctl(robot):
    controller = RobotController(robot)
    controller.schedule = lambda delay, fn, *args: None  # no servo relax timers in tests
    controller.sleep = lambda seconds: None
    yield controller
    controller.shutdown()


@pytest.fixture
def api(ctl):
    server, address = serve(create_app(ctl))
    client = ApiClient(address)
    yield client
    client.close()
    server.shutdown()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # closed again: nothing listens there


# ---------- happy paths ----------

def test_status(api):
    r = api.status()
    assert r.ok and r.status == 200 and r.error is None
    assert r.data["cmd"] == "stop"
    assert "distance_cm" in r.data and "obstacle" in r.data
    climate = parse_climate(r.data)  # this test robot has no monitor: reported as turned off
    assert climate.available is False and "CLIMATE_ENABLED" in climate.reason


def test_config(api):
    r = api.config()
    assert r.ok
    assert r.data["camera_stream_url"] == "http://127.0.0.1:%d/?action=stream" % config.CAMERA_STREAM_PORT
    assert r.data["servo"]["PAN_LEFT"] == config.PAN_LEFT
    assert r.data["servo"]["TILT_UP"] == config.TILT_UP
    assert r.data["watchdog_timeout_s"] == config.WATCHDOG_TIMEOUT_S


def test_drive(api, robot):
    r = api.drive("forward", 60)
    assert r.ok and r.data["speed"] == 60
    assert robot.last_cmd == "forward"
    assert api.drive("stop").ok
    assert robot.last_cmd == "stop"


def test_camera_front_beep_led(api, ctl):
    r = api.camera(pan=config.PAN_LEFT)
    assert r.ok and r.data["pan"] == config.PAN_LEFT and ctl.pan == config.PAN_LEFT
    assert api.front_servo(config.FRONT_RIGHT).data["angle"] == config.FRONT_RIGHT
    assert api.beep(0.1).data["seconds"] == 0.1
    assert api.led(255, 0, 0).data["led"] == [255, 0, 0]


def test_mode(api, ctl):
    r = api.mode("avoid")
    assert r.ok and r.data["mode"] == "avoid" and ctl.mode == "avoid"
    assert api.drive("stop").ok                            # manual command ends auto
    assert ctl.mode == "manual"
    bad = api.mode("fly")
    assert not bad.ok and bad.status == 400


def test_scan(api, ctl):
    r = api.scan()
    assert r.ok
    assert r.data["angles"] == list(config.SCAN_ANGLES)
    assert len(r.data["distances_cm"]) == len(config.SCAN_ANGLES)


# ---------- server errors come back as clear messages ----------

def test_drive_blocked_409(api, ctl):
    ctl.obstacle = True
    r = api.drive("forward", 50)
    assert not r.ok
    assert r.status == 409
    assert "obstacle" in r.error


def test_bad_command_400(api):
    r = api.drive("fly")
    assert not r.ok and r.status == 400
    assert "fly" in r.error or "forward" in r.error


def test_camera_needs_pan_or_tilt(api):
    r = api.camera()
    assert r.status == 400 and "pan" in r.error


# ---------- network errors never raise ----------

def test_unreachable_robot():
    client = ApiClient("127.0.0.1:%d" % free_port())
    r = client.status()
    assert not r.ok and r.status is None
    assert "cannot reach robot" in r.error


def test_timeout(api, ctl):
    ctl.status = lambda: time.sleep(1.0)  # server answers too late
    started = time.monotonic()
    r = api.status()
    assert not r.ok and r.status is None
    assert "did not answer" in r.error
    assert time.monotonic() - started < 0.9  # gave up after ~0.5 s, didn't wait the full 1 s


def test_not_json_reply():
    def html_app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/html")])
        return [b"<html>RaspAP</html>"]

    server, address = serve(html_app)
    try:
        r = ApiClient(address).status()
        assert not r.ok and r.status == 200
        assert "not JSON" in r.error
    finally:
        server.shutdown()


# ---------- address box input ----------

@pytest.mark.parametrize("text, url", [
    ("192.168.50.1:5000", "http://192.168.50.1:5000"),
    ("  127.0.0.1:5000/ ", "http://127.0.0.1:5000"),
    ("http://127.0.0.1:5000", "http://127.0.0.1:5000"),
])
def test_normalize_address(text, url):
    assert normalize_address(text) == url
