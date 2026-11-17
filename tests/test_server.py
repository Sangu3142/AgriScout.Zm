import json
import time

import pytest

import climate
import config
import soil
from fake_hardware import (FakeDht11, FakeRobot, FakeSoil, fake_connect_pigpio,
                           fake_find_soil_ports)
from robot_server import RobotController, create_app


class Recorder(object):
    """Stands in for controller.schedule: remembers timers instead of starting them."""

    def __init__(self):
        self.timers = []

    def __call__(self, delay, fn, *args):
        self.timers.append((delay, fn, args))

    def fire(self, index):
        _, fn, args = self.timers[index]
        fn(*args)


@pytest.fixture
def robot():
    return FakeRobot()


@pytest.fixture
def ctl(robot):
    controller = RobotController(robot)
    controller.schedule = Recorder()
    controller.sleep = lambda seconds: None  # no real waiting in tests
    yield controller
    controller.shutdown()


@pytest.fixture
def client(ctl):
    return create_app(ctl).test_client()


def post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


def set_distance(robot, cm):
    robot.read_distance_cm = lambda: cm


# ---------- happy paths ----------

def test_drive(client, robot, ctl):
    r = post(client, "/api/drive", {"cmd": "forward", "speed": 60})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "cmd": "forward", "speed": 60}
    assert robot.last_cmd == "forward" and robot.speed == 60
    assert ctl.cmd == "forward"


def test_drive_default_speed_and_clamp(client, robot):
    assert post(client, "/api/drive", {"cmd": "back"}).get_json()["speed"] == config.DEFAULT_SPEED
    assert post(client, "/api/drive", {"cmd": "back", "speed": 500}).get_json()["speed"] == 100
    assert post(client, "/api/drive", {"cmd": "stop", "speed": 80}).get_json()["speed"] == 0


def test_drive_without_content_type(client, robot):
    r = client.post("/api/drive", data='{"cmd": "left"}')  # like a lazy curl
    assert r.status_code == 200
    assert robot.last_cmd == "left"


def test_camera(client, robot):
    r = post(client, "/api/camera", {"pan": 30, "tilt": 10})
    assert r.get_json() == {"ok": True, "pan": 30, "tilt": 45}  # tilt clamped to TILT_MIN
    assert (robot.pan, robot.tilt) == (30, 45)
    r = post(client, "/api/camera", {"pan": 120})  # tilt kept
    assert r.get_json() == {"ok": True, "pan": 120, "tilt": 45}


def test_front_servo(client, robot):
    r = post(client, "/api/front_servo", {"angle": config.FRONT_LEFT})
    assert r.get_json() == {"ok": True, "angle": config.FRONT_LEFT}
    assert robot.front == config.FRONT_LEFT


def test_beep(client, robot, ctl):
    r = post(client, "/api/beep", {"seconds": 0.05})
    assert r.get_json() == {"ok": True, "seconds": 0.05}
    ctl._beep_thread.join(1.0)
    assert robot.calls.count("buzzer") == 2  # on, then off
    assert not robot.buzzing


def test_beep_clamped(client):
    assert post(client, "/api/beep", {"seconds": 99}).get_json()["seconds"] == config.BEEP_MAX_S


def test_led(client, robot):
    r = post(client, "/api/led", {"r": 300, "g": 0, "b": 128})
    assert r.get_json() == {"ok": True, "led": [255, 0, 128]}
    assert robot.led == (255, 0, 128)


def test_status(client, ctl):
    set_distance(ctl.robot, 42.0)
    ctl.sensor_tick()
    post(client, "/api/drive", {"cmd": "spin_left", "speed": 30})
    s = client.get("/api/status").get_json()
    assert set(s) == {"distance_cm", "line", "cmd", "speed", "pan", "tilt", "front",
                      "mode", "obstacle", "uptime_s",
                      "mode_reason", "front_ahead", "scanning", "led",  # phase 4a
                      "climate",                                         # DHT11 phase 3
                      "soil"}                                            # Uno soil probe
    assert s["distance_cm"] == 42.0
    assert set(s["line"]) == {"L1", "L2", "R1", "R2"}
    assert s["cmd"] == "spin_left" and s["speed"] == 30
    assert s["mode"] == "manual" and s["obstacle"] is False
    assert s["uptime_s"] >= 0


CLIMATE_KEYS = {"available", "temp_c", "humidity_pct", "age_s", "ok", "failed", "last_error",
                "reason"}


def test_status_climate_without_monitor_is_off(client):
    c = client.get("/api/status").get_json()["climate"]
    assert set(c) == CLIMATE_KEYS
    assert c["available"] is False and c["temp_c"] is None and "CLIMATE_ENABLED" in c["reason"]


def test_status_climate_with_fake_sensor(robot):
    monitor = climate.ClimateMonitor(fake_connect_pigpio, FakeDht11)
    ctl = RobotController(robot, monitor)
    try:
        monitor.tick(ctl.clock())  # power-on read, thrown away
        monitor.tick(ctl.clock())
        c = create_app(ctl).test_client().get("/api/status").get_json()["climate"]
        assert set(c) == CLIMATE_KEYS
        assert c["available"] is True and c["reason"] is None
        assert (c["temp_c"], c["humidity_pct"]) == (24.0, 60.0)
        assert c["ok"] == 1 and c["failed"] == 0 and 0 <= c["age_s"] < 5
    finally:
        ctl.shutdown()
    assert monitor.snapshot(0.0)["available"] is False  # shutdown closed the sensor


def test_shutdown_closes_climate_even_if_gpio_cleanup_fails(robot):
    closed = []
    monitor = climate.DisabledClimate()
    monitor.close = lambda: closed.append(1)

    def broken_cleanup():
        raise RuntimeError("GPIO gone")
    robot.cleanup = broken_cleanup
    ctl = RobotController(robot, monitor)
    with pytest.raises(RuntimeError):
        ctl.shutdown()
    assert closed == [1]


SOIL_KEYS = {"available", "raw", "moisture_pct", "state", "age_s", "reason"}


def fake_soil_monitor():
    return soil.SoilMonitor(lambda path: FakeSoil(path, sleep=lambda s: None),
                            fake_find_soil_ports)


def test_status_soil_without_monitor_is_off(client):
    s = client.get("/api/status").get_json()["soil"]
    assert set(s) == SOIL_KEYS
    assert s["available"] is False and s["raw"] is None and "SOIL_ENABLED" in s["reason"]


def test_status_soil_with_fake_uno(robot):
    monitor = fake_soil_monitor()
    ctl = RobotController(robot, soil_monitor=monitor)
    try:
        for _ in range(3):              # open, READY, SOIL 600
            monitor.tick(ctl.clock())
        s = create_app(ctl).test_client().get("/api/status").get_json()["soil"]
        assert set(s) == SOIL_KEYS
        assert (s["available"], s["raw"], s["moisture_pct"], s["state"], s["reason"]) == \
            (True, 600, 57, "ok", None)
    finally:
        ctl.shutdown()
    assert monitor.snapshot(0.0)["reason"] == "robot server stopped"  # shutdown closed the port


def test_dry_soil_changes_nothing_else(robot):
    # Soil is information only: no LED, mode or drive change, whatever it reads.
    port = FakeSoil("FAKE", sleep=lambda s: None)
    port.readline = lambda: b"SOIL 1023\r\n"
    monitor = soil.SoilMonitor(lambda path: port, fake_find_soil_ports)
    ctl = RobotController(robot, soil_monitor=monitor)
    client = create_app(ctl).test_client()
    try:
        before = client.get("/api/status").get_json()
        monitor.tick(ctl.clock())
        monitor.tick(ctl.clock())
        after = client.get("/api/status").get_json()
        assert after["soil"]["state"] == "dry"
        for key in ("led", "mode", "cmd", "speed", "obstacle"):
            assert after[key] == before[key], key
        assert robot.led == (0, 0, 0) and robot.last_cmd == "stop"
    finally:
        ctl.shutdown()


def test_shutdown_closes_soil_even_if_climate_close_fails(robot):
    closed = []
    broken_climate = climate.DisabledClimate()

    def broken_close():
        raise RuntimeError("pigpiod gone")
    broken_climate.close = broken_close
    monitor = soil.DisabledSoil()
    monitor.close = lambda: closed.append(1)
    ctl = RobotController(robot, broken_climate, monitor)
    with pytest.raises(RuntimeError):
        ctl.shutdown()
    assert closed == [1]


def test_config(client):
    c = client.get("/api/config", base_url="http://192.168.50.1:5000").get_json()
    assert c["camera_stream_url"] == "http://192.168.50.1:8080/?action=stream"
    assert c["servo"]["PAN_LEFT"] == config.PAN_LEFT
    assert c["servo"]["FRONT_LEFT"] == config.FRONT_LEFT
    assert "forward" in c["drive_commands"]
    assert c["watchdog_timeout_s"] == config.WATCHDOG_TIMEOUT_S


# ---------- bad input -> 400, never a crash ----------

@pytest.mark.parametrize("url, raw", [
    ("/api/drive", "not json"),
    ("/api/drive", "[1, 2]"),
    ("/api/drive", "{}"),                                  # missing cmd
    ("/api/drive", '{"cmd": "fly"}'),                      # unknown cmd
    ("/api/drive", '{"cmd": 5}'),
    ("/api/drive", '{"cmd": "forward", "speed": "fast"}'),
    ("/api/drive", '{"cmd": "forward", "speed": true}'),   # bool is not a number here
    ("/api/drive", '{"cmd": "forward", "speed": NaN}'),
    ("/api/camera", "{}"),
    ("/api/camera", '{"pan": "x"}'),
    ("/api/front_servo", "{}"),
    ("/api/beep", '{"seconds": null}'),
    ("/api/led", '{"r": 1, "g": 2}'),                      # missing b
])
def test_bad_input_400(client, robot, url, raw):
    r = client.post(url, data=raw, content_type="application/json")
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False and body["error"]
    assert robot.last_cmd == "stop"  # nothing moved


def test_unknown_cmd_lists_valid_ones(client):
    error = post(client, "/api/drive", {"cmd": "fly"}).get_json()["error"]
    assert "spin_left" in error


def test_404_and_405_are_json(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.get_json()["ok"] is False
    r = client.get("/api/drive")
    assert r.status_code == 405 and r.get_json()["ok"] is False


# ---------- watchdog ----------

def test_watchdog_stops_after_timeout(client, robot, ctl):
    post(client, "/api/drive", {"cmd": "forward"})
    t0 = ctl.last_drive_time
    ctl.watchdog_tick(t0 + config.WATCHDOG_TIMEOUT_S - 0.1)
    assert robot.last_cmd == "forward"
    ctl.watchdog_tick(t0 + config.WATCHDOG_TIMEOUT_S + 0.1)
    assert robot.last_cmd == "stop"
    assert ctl.cmd == "stop"


def test_watchdog_kept_alive_by_repeated_commands(client, robot, ctl):
    post(client, "/api/drive", {"cmd": "forward"})
    ctl.watchdog_tick(ctl.last_drive_time + config.WATCHDOG_TIMEOUT_S - 0.1)
    post(client, "/api/drive", {"cmd": "forward"})       # resets the timer
    ctl.watchdog_tick(ctl.last_drive_time + config.WATCHDOG_TIMEOUT_S - 0.1)
    assert robot.last_cmd == "forward"


def test_watchdog_real_threads(client, robot, ctl):
    set_distance(robot, 100.0)
    ctl.start_threads()
    post(client, "/api/drive", {"cmd": "forward"})
    time.sleep(config.WATCHDOG_TIMEOUT_S + 0.3)
    assert robot.last_cmd == "stop"


# ---------- obstacle stop ----------

def test_obstacle_blocks_forward_allows_back(client, robot, ctl):
    post(client, "/api/drive", {"cmd": "forward"})
    set_distance(robot, 10.0)
    ctl.sensor_tick()
    assert robot.last_cmd == "stop"
    assert ctl.obstacle is True
    ctl._beep_thread.join(1.0)
    assert "buzzer" in robot.calls                        # warning beep

    r = post(client, "/api/drive", {"cmd": "forward"})
    assert r.status_code == 409 and r.get_json()["ok"] is False
    assert robot.last_cmd == "stop"

    r = post(client, "/api/drive", {"cmd": "back"})
    assert r.status_code == 200 and robot.last_cmd == "back"
    ctl.sensor_tick()                                     # still close, but backing is fine
    assert robot.last_cmd == "back"

    set_distance(robot, 25.0)
    for _ in range(config.OBSTACLE_CLEAR_COUNT):
        ctl.sensor_tick()
    assert ctl.obstacle is False
    assert post(client, "/api/drive", {"cmd": "forward"}).status_code == 200


def test_obstacle_only_while_forward(client, robot, ctl):
    set_distance(robot, 10.0)
    post(client, "/api/drive", {"cmd": "spin_right"})
    ctl.sensor_tick()
    assert robot.last_cmd == "spin_right"
    assert ctl.obstacle is False


def test_no_echo_never_clears_obstacle(client, robot, ctl):
    post(client, "/api/drive", {"cmd": "forward"})
    set_distance(robot, 10.0)
    ctl.sensor_tick()
    assert ctl.obstacle is True

    set_distance(robot, None)                             # no echo: object very close or at an angle
    ctl.sensor_tick()
    assert ctl.obstacle is True
    assert post(client, "/api/drive", {"cmd": "forward"}).status_code == 409

    set_distance(robot, 18.0)                             # between 15 and 20: hysteresis keeps it
    ctl.sensor_tick()
    assert ctl.obstacle is True

    set_distance(robot, 25.0)                             # real clear readings, enough in a row
    for _ in range(config.OBSTACLE_CLEAR_COUNT):
        ctl.sensor_tick()
    assert ctl.obstacle is False


def test_no_echo_does_not_set_obstacle(client, robot, ctl):
    post(client, "/api/drive", {"cmd": "forward"})
    set_distance(robot, None)
    ctl.sensor_tick()
    assert ctl.obstacle is False
    assert robot.last_cmd == "forward"


def feed(robot, ctl, readings):
    """Run one sensor tick per reading."""
    for cm in readings:
        set_distance(robot, cm)
        ctl.sensor_tick()


def test_obstacle_at_3_5_cm_while_forward(client, robot, ctl):
    set_distance(robot, 100.0)
    post(client, "/api/drive", {"cmd": "forward"})
    feed(robot, ctl, [3.5])
    assert robot.last_cmd == "stop"
    assert ctl.obstacle is True
    ctl._beep_thread.join(1.0)
    assert robot.calls.count("buzzer") == 2
    assert post(client, "/api/drive", {"cmd": "forward"}).status_code == 409


def test_forward_refused_when_obstacle_already_there(client, robot, ctl):
    set_distance(robot, 3.5)                              # hand already in front, tank stopped
    robot.calls = []
    r = post(client, "/api/drive", {"cmd": "forward"})
    assert r.status_code == 409
    assert "drive" not in robot.calls                     # motors never started
    assert robot.last_cmd == "stop"
    assert ctl.obstacle is True
    ctl._beep_thread.join(1.0)
    assert "buzzer" in robot.calls
    assert post(client, "/api/drive", {"cmd": "back"}).status_code == 200


def test_tank_run1_spike_does_not_clear(client, robot, ctl):
    # Real readings from the tank (hand at ~4-5 cm). One false 51.1 used to clear the flag.
    set_distance(robot, 100.0)
    post(client, "/api/drive", {"cmd": "forward"})
    feed(robot, ctl, [8.5, 4.0, 4.0, 4.0, 51.1, 5.0])
    assert ctl.obstacle is True
    assert post(client, "/api/drive", {"cmd": "forward"}).status_code == 409


def test_clear_needs_readings_in_a_row(client, robot, ctl):
    set_distance(robot, 100.0)
    post(client, "/api/drive", {"cmd": "forward"})
    almost = [30.0] * (config.OBSTACLE_CLEAR_COUNT - 1)
    feed(robot, ctl, [3.5] + almost)
    assert ctl.obstacle is True                           # one short
    feed(robot, ctl, [None] + almost)                     # no echo restarts the count
    assert ctl.obstacle is True
    feed(robot, ctl, [18.0] + almost)                     # 15-20 cm restarts it too
    assert ctl.obstacle is True
    feed(robot, ctl, [30.0])                              # last one in a row
    assert ctl.obstacle is False


@pytest.mark.parametrize("near_cm", [12.0, 30.0])
def test_blind_stop_after_near_reading(client, robot, ctl, near_cm):
    feed(robot, ctl, [near_cm])                           # last real reading, tank stopped
    set_distance(robot, None)
    post(client, "/api/drive", {"cmd": "forward"})        # fresh check: no echo #1, allowed
    assert robot.last_cmd == "forward"
    feed(robot, ctl, [None])                              # #2
    assert robot.last_cmd == "forward"
    feed(robot, ctl, [None])                              # #3 -> blind stop
    assert robot.last_cmd == "stop"
    assert ctl.obstacle is False                          # blind stop sets no flag


def test_no_blind_stop_after_far_reading(client, robot, ctl):
    feed(robot, ctl, [150.0])                             # open space
    set_distance(robot, None)
    post(client, "/api/drive", {"cmd": "forward"})
    feed(robot, ctl, [None, None, None, None])
    assert robot.last_cmd == "forward"
    assert ctl.obstacle is False


def test_no_blind_stop_while_backing(client, robot, ctl):
    feed(robot, ctl, [12.0])
    post(client, "/api/drive", {"cmd": "back"})
    feed(robot, ctl, [None, None, None])
    assert robot.last_cmd == "back"


# ---------- forward re-centres the ultrasonic ----------

def test_forward_recentres_front_servo_first(client, robot, ctl):
    post(client, "/api/front_servo", {"angle": 170})
    robot.calls = []
    ctl.schedule.timers = []

    r = post(client, "/api/drive", {"cmd": "forward"})
    assert r.status_code == 200
    assert robot.front == config.FRONT_CENTER
    assert client.get("/api/status").get_json()["front"] == config.FRONT_CENTER
    assert robot.last_cmd == "forward"
    assert robot.calls.index("set_front_servo") < robot.calls.index("drive")
    assert len(ctl.schedule.timers) == 1                  # relax for the front servo


def test_forward_recentre_waits_for_servo(client, robot, ctl):
    waits = []
    ctl.sleep = waits.append
    post(client, "/api/front_servo", {"angle": 170})
    post(client, "/api/drive", {"cmd": "forward"})
    assert waits == [config.SERVO_SETTLE_S]


def test_forward_within_tolerance_does_not_move_servo(client, robot, ctl):
    post(client, "/api/front_servo", {"angle": 100})      # 10 degrees off: inside 15
    robot.calls = []
    ctl.schedule.timers = []
    post(client, "/api/drive", {"cmd": "forward"})
    assert robot.front == 100
    assert "set_front_servo" not in robot.calls
    assert ctl.schedule.timers == []


def test_back_does_not_recentre(client, robot):
    post(client, "/api/front_servo", {"angle": 170})
    post(client, "/api/drive", {"cmd": "back"})
    assert robot.front == 170


# ---------- servo relax never cancels a newer move ----------

def test_stale_relax_does_not_cancel_newer_move(client, robot, ctl):
    post(client, "/api/camera", {"pan": 30})              # move A
    post(client, "/api/camera", {"pan": 150})             # move B
    assert len(ctl.schedule.timers) == 2
    assert all(d == config.SERVO_SETTLE_S for d, _, _ in ctl.schedule.timers)

    ctl.schedule.fire(0)                                  # A's timer: B is newer -> skip
    assert "pan" not in robot.relaxed
    assert "tilt" not in robot.relaxed

    ctl.schedule.fire(1)                                  # B's timer: latest -> relax
    assert "pan" in robot.relaxed
    assert "tilt" in robot.relaxed


def test_relax_counters_are_per_servo(client, robot, ctl):
    post(client, "/api/camera", {"pan": 30})
    post(client, "/api/front_servo", {"angle": 20})
    ctl.schedule.fire(0)                                  # camera timer
    assert {"pan", "tilt"} <= robot.relaxed
    assert "front" not in robot.relaxed                   # front's own move is still running
    ctl.schedule.fire(1)
    assert "front" in robot.relaxed


def test_moving_again_unrelaxes(client, robot, ctl):
    post(client, "/api/camera", {"pan": 30})
    ctl.schedule.fire(0)
    assert "pan" in robot.relaxed
    post(client, "/api/camera", {"pan": 60})
    assert "pan" not in robot.relaxed


def test_relax_after_shutdown_is_ignored(client, robot, ctl):
    post(client, "/api/camera", {"pan": 30})
    ctl.shutdown()
    robot.calls = []
    ctl.schedule.fire(0)
    assert robot.calls == []


def test_real_timer_relaxes(robot):
    ctl = RobotController(robot)                          # default schedule = real Timer
    try:
        ctl.set_camera(30, 90)
        assert "pan" not in robot.relaxed
        time.sleep(config.SERVO_SETTLE_S + 0.2)
        assert "pan" in robot.relaxed
    finally:
        ctl.shutdown()


def test_home_servos(robot, ctl):
    ctl.home_servos()
    assert (robot.pan, robot.tilt, robot.front) == (config.PAN_CENTER, config.TILT_LEVEL,
                                                    config.FRONT_CENTER)


# ---------- shutdown ----------

def test_shutdown_stops_and_cleans_up(client, robot, ctl):
    post(client, "/api/drive", {"cmd": "forward"})
    ctl.shutdown()
    ctl.shutdown()                                        # safe twice
    assert robot.last_cmd == "stop"
    assert robot.cleaned_up
    assert post(client, "/api/drive", {"cmd": "back"}).status_code == 503
