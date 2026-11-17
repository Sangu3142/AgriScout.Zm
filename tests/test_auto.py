"""Phase 4a: auto modes, heartbeat, status LED, ultrasonic pointing and scan. FakeRobot + fake time."""

import json

import pytest

import config
import shared
from fake_hardware import FakeRobot
from robot_server import RobotController, create_app

WHITE = (False, False, False, False)
CENTRED = (False, True, True, False)  # L1 L2 R1 R2: middle pair on the tape


class Clock(object):
    """Fake time: only moves when a test (or ctl.sleep) moves it."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture
def robot():
    r = FakeRobot()
    r.read_distance_cm = lambda: 100.0
    return r


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ctl(robot, clock):
    c = RobotController(robot)
    c.schedule = lambda delay, fn, *args: None  # no servo relax timers
    c.clock = clock
    c.sleep = clock.sleep
    c.last_heartbeat = c.last_drive_time = c.start_time = clock()
    c.beeps = []
    c.beep = c.beeps.append  # record beeps instead of starting buzzer threads
    yield c
    c.shutdown()


@pytest.fixture
def client(ctl):
    return create_app(ctl).test_client()


def post(client, url, body=None):
    return client.post(url, data=json.dumps(body if body is not None else {}),
                       content_type="application/json")


def set_line(robot, values):
    robot.read_line_sensors = lambda: dict(zip(("L1", "L2", "R1", "R2"), values))


def set_distances(robot, by_angle):
    """Distance depends on where the ultrasonic points: {angle: cm}."""
    robot.read_distance_cm = lambda: by_angle.get(robot.front)


def step(ctl, clock, n=1, dt=config.AUTO_PERIOD_S, alive=True):
    """Advance time like the real threads: heartbeat (GUI polling), sensor reading, auto step."""
    for _ in range(n):
        clock.t += dt
        if alive:
            ctl.heartbeat()
        ctl.sensor_tick()
        ctl.auto_tick()


def record_drives(robot):
    cmds = []
    original = robot.drive

    def drive(cmd, speed=config.DEFAULT_SPEED):
        cmds.append(cmd)
        return original(cmd, speed)
    robot.drive = drive
    return cmds


# ---------- /api/mode ----------

def test_mode_api(client, ctl):
    r = post(client, "/api/mode", {"mode": "line_follow"})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "mode": "line_follow"}
    assert client.get("/api/status").get_json()["mode"] == "line_follow"
    assert post(client, "/api/mode", {"mode": "manual"}).get_json()["mode"] == "manual"
    assert ctl.auto is None


@pytest.mark.parametrize("body", [{"mode": "fly"}, {}, {"mode": 5}, {"mode": None}])
def test_mode_api_rejects_bad_mode(client, body):
    r = post(client, "/api/mode", body)
    assert r.status_code == 400
    assert "line_follow" in r.get_json()["error"]


def test_line_follow_refused_while_obstacle(client, ctl):
    ctl.obstacle = True
    r = post(client, "/api/mode", {"mode": "line_follow"})
    assert r.status_code == 409 and "obstacle" in r.get_json()["error"]
    assert ctl.mode == "manual"
    assert post(client, "/api/mode", {"mode": "avoid"}).status_code == 200  # escaping is its job


def test_config_lists_modes_and_scan_angles(client):
    c = client.get("/api/config").get_json()
    assert c["modes"] == ["manual", "line_follow", "avoid"]
    assert c["scan_angles"][0] == config.FRONT_LEFT and c["scan_angles"][-1] == config.FRONT_RIGHT


def test_auto_start_centres_ultrasonic_and_waits(client, ctl, robot, clock):
    robot.front = ctl.front = config.FRONT_LEFT
    post(client, "/api/mode", {"mode": "line_follow"})
    assert robot.front == config.FRONT_CENTER
    set_line(robot, CENTRED)
    ctl.auto_tick(clock() + config.SERVO_SETTLE_S - 0.01)
    assert robot.last_cmd == "stop"                        # servo still moving
    ctl.auto_tick(clock() + config.SERVO_SETTLE_S)
    assert robot.last_cmd == "forward"


# ---------- line follow ----------

T, F = True, False


@pytest.mark.parametrize("sensors, cmd, sharp", [
    ((F, F, F, T), "spin_right", False),    # rule 4: far right
    ((T, F, F, F), "spin_left", False),     # rule 3: far left
    ((F, T, F, T), "spin_right", True),     # rule 1: sharp right
    ((T, T, T, T), "spin_right", True),     # rule 1: all black (T-junction turns right)
    ((T, F, T, F), "spin_left", True),      # rule 2: sharp left
    ((T, T, F, F), "spin_left", False),     # rule 3
    ((F, T, F, F), "left", False),          # rule 5: drifted right
    ((F, F, T, F), "right", False),         # rule 6: drifted left
    ((F, T, T, F), "forward", False),       # rule 7: centred
    ((F, F, F, F), None, False),            # all white: keep previous
])
def test_line_command_truth_table(sensors, cmd, sharp):
    got_cmd, hold = shared.line_command(dict(zip(("L1", "L2", "R1", "R2"), sensors)))
    assert got_cmd == cmd
    assert (hold > 0) == sharp


def test_line_follow_drives_with_line_speeds(client, ctl, robot, clock):
    post(client, "/api/mode", {"mode": "line_follow"})
    for sensors, cmd, speed in ((CENTRED, "forward", config.LINE_SPEED),
                                ((F, T, F, F), "left", config.LINE_TURN_SPEED),
                                ((F, F, T, F), "right", config.LINE_TURN_SPEED),
                                ((T, F, F, F), "spin_left", config.LINE_SPIN_SPEED)):
        set_line(robot, sensors)
        step(ctl, clock)
        assert (robot.last_cmd, robot.speed) == (cmd, speed)


def test_sharp_turn_is_held(client, ctl, robot, clock):
    post(client, "/api/mode", {"mode": "line_follow"})
    set_line(robot, (F, T, F, T))                          # sharp right
    ctl.auto_tick(clock.t)
    assert robot.last_cmd == "spin_right"
    set_line(robot, CENTRED)
    ctl.auto_tick(clock.t + 0.05)                          # inside the 0.08 s hold
    assert robot.last_cmd == "spin_right"
    ctl.auto_tick(clock.t + 0.1)
    assert robot.last_cmd == "forward"


def test_line_lost_after_1_s(client, ctl, robot, clock):
    post(client, "/api/mode", {"mode": "line_follow"})
    set_line(robot, CENTRED)
    t0 = clock.t
    ctl.auto_tick(t0)
    set_line(robot, WHITE)
    ctl.auto_tick(t0 + config.LINE_LOST_S - 0.1)
    assert ctl.mode == "line_follow" and robot.last_cmd == "forward"  # keeps going, like Yahboom
    assert ctl.beeps == []
    ctl.auto_tick(t0 + config.LINE_LOST_S)
    assert robot.last_cmd == "stop"
    assert ctl.mode == "manual" and ctl.mode_reason == "line lost"
    assert ctl.beeps == [config.OBSTACLE_BEEP_S]
    ctl.led_tick(t0 + 2)
    assert robot.led == config.LED_LINE_LOST
    post(client, "/api/mode", {"mode": "manual"})           # mode change clears yellow
    ctl.led_tick(t0 + 3)
    assert robot.led == config.LED_MANUAL


# ---------- manual always wins ----------

@pytest.mark.parametrize("cmd", ["stop", "back", "forward", "spin_left"])
def test_manual_command_cancels_auto(client, ctl, robot, clock, cmd):
    post(client, "/api/mode", {"mode": "line_follow"})
    set_line(robot, CENTRED)
    step(ctl, clock)
    assert robot.last_cmd == "forward"
    assert post(client, "/api/drive", {"cmd": cmd}).status_code == 200
    assert ctl.mode == "manual" and ctl.auto is None
    assert ctl.mode_reason == "manual command"
    assert robot.last_cmd == cmd
    step(ctl, clock, 5)                                    # auto thread keeps ticking: no effect
    assert robot.last_cmd == cmd


def test_old_auto_object_cannot_drive_after_cancel(client, ctl, robot):
    post(client, "/api/mode", {"mode": "avoid"})
    old = ctl.auto
    post(client, "/api/drive", {"cmd": "stop"})
    assert ctl.auto_drive(old, "forward", 50) is False
    assert robot.last_cmd == "stop"


def test_watchdog_ignores_auto_but_protects_manual(client, ctl, robot, clock):
    post(client, "/api/mode", {"mode": "line_follow"})
    set_line(robot, CENTRED)
    step(ctl, clock)
    ctl.watchdog_tick(clock.t + 2 * config.WATCHDOG_TIMEOUT_S)
    assert robot.last_cmd == "forward"                     # auto sends no Wi-Fi drive commands
    post(client, "/api/drive", {"cmd": "forward"})         # manual now
    ctl.watchdog_tick(ctl.last_drive_time + config.WATCHDOG_TIMEOUT_S + 0.1)
    assert robot.last_cmd == "stop"


# ---------- heartbeat ----------

def test_heartbeat_lost_returns_to_manual(client, ctl, robot, clock):
    post(client, "/api/mode", {"mode": "line_follow"})
    set_line(robot, CENTRED)
    t0 = ctl.last_heartbeat
    ctl.auto_tick(t0 + config.AUTO_HEARTBEAT_S - 0.1)
    assert ctl.mode == "line_follow" and robot.last_cmd == "forward"
    ctl.auto_tick(t0 + config.AUTO_HEARTBEAT_S + 0.1)
    assert ctl.mode == "manual" and ctl.mode_reason == "heartbeat lost"
    assert robot.last_cmd == "stop"
    assert ctl.beeps


def test_status_polling_is_the_heartbeat(client, ctl, robot, clock):
    post(client, "/api/mode", {"mode": "line_follow"})
    set_line(robot, CENTRED)
    t0 = clock.t
    clock.t = t0 + 2.0
    client.get("/api/status")                              # GUI poll
    ctl.auto_tick(t0 + 4.0)                                # 4 s after start, 2 s after the poll
    assert ctl.mode == "line_follow"


# ---------- obstacle during auto ----------

def test_obstacle_during_line_follow(client, ctl, robot, clock):
    post(client, "/api/mode", {"mode": "line_follow"})
    set_line(robot, CENTRED)
    step(ctl, clock)
    assert robot.last_cmd == "forward"
    robot.read_distance_cm = lambda: 10.0
    ctl.sensor_tick()
    assert robot.last_cmd == "stop" and ctl.obstacle is True
    ctl.auto_tick(clock.t)
    assert ctl.mode == "manual" and ctl.mode_reason == "obstacle"   # decision A
    step(ctl, clock, 3)
    assert robot.last_cmd == "stop"


def test_left_counts_as_forward_in_auto(client, ctl, robot, clock):
    post(client, "/api/mode", {"mode": "line_follow"})
    set_line(robot, (F, T, F, F))                          # -> "left" (one track forward)
    step(ctl, clock)
    assert robot.last_cmd == "left"
    robot.read_distance_cm = lambda: 10.0
    ctl.sensor_tick()
    assert robot.last_cmd == "stop" and ctl.obstacle is True


def test_obstacle_led_flashes_red(ctl, robot):
    ctl.obstacle = True
    ctl.led_tick(0.0)
    assert robot.led == config.LED_OBSTACLE
    ctl.led_tick(config.LED_FLASH_S)
    assert robot.led == config.LED_OFF
    ctl.led_tick(2 * config.LED_FLASH_S)
    assert robot.led == config.LED_OBSTACLE


# ---------- LED ----------

def test_led_colour_per_state(client, ctl, robot, clock):
    ctl.led_tick(clock.t)
    assert robot.led == config.LED_MANUAL
    post(client, "/api/mode", {"mode": "avoid"})
    ctl.led_tick(clock.t)
    assert robot.led == config.LED_AUTO
    post(client, "/api/led", {"r": 10, "g": 20, "b": 30})  # manual override...
    ctl.led_tick(clock.t)
    assert robot.led == (10, 20, 30)
    ctl.obstacle = True                                    # ...but the obstacle flash wins
    ctl.led_tick(0.0)
    assert robot.led == config.LED_OBSTACLE
    ctl.obstacle = False
    ctl.led_tick(clock.t)
    assert robot.led == (10, 20, 30)
    post(client, "/api/mode", {"mode": "manual"})           # mode change ends the override
    ctl.led_tick(clock.t)
    assert robot.led == config.LED_MANUAL
    assert client.get("/api/status").get_json()["led"] == list(config.LED_MANUAL)


def test_led_only_written_on_change(ctl, robot, clock):
    writes = []
    original = robot.set_led
    robot.set_led = lambda r, g, b: writes.append((r, g, b)) or original(r, g, b)
    for _ in range(5):
        ctl.led_tick(clock.t)
    assert writes == [config.LED_MANUAL]


# ---------- ultrasonic pointing: side readings are ignored ----------

def test_side_reading_does_not_set_obstacle(ctl, robot):
    ctl.set_front_servo(config.FRONT_LEFT)
    ctl.cmd = "forward"                                    # moving forward, sensor looking left
    robot.read_distance_cm = lambda: 5.0
    ctl.sensor_tick()
    assert ctl.obstacle is False
    assert ctl.distance_cm == 5.0                          # still shown in the GUI
    assert robot.last_cmd == "stop"                        # (motors untouched by this tick)


def test_side_readings_do_not_clear_obstacle(ctl, robot):
    ctl.obstacle = True
    ctl.set_front_servo(config.FRONT_RIGHT)
    robot.read_distance_cm = lambda: 100.0
    for _ in range(config.OBSTACLE_CLEAR_COUNT + 3):
        ctl.sensor_tick()
    assert ctl.obstacle is True
    ctl.set_front_servo(config.FRONT_CENTER)               # looking ahead again: count starts at 0
    for _ in range(config.OBSTACLE_CLEAR_COUNT - 1):
        ctl.sensor_tick()
    assert ctl.obstacle is True
    ctl.sensor_tick()
    assert ctl.obstacle is False


def test_servo_move_restarts_clear_count(ctl, robot):
    ctl.obstacle = True
    robot.read_distance_cm = lambda: 100.0
    for _ in range(config.OBSTACLE_CLEAR_COUNT - 1):
        ctl.sensor_tick()
    ctl.set_front_servo(config.FRONT_CENTER + 10)          # small move, still "ahead"
    ctl.sensor_tick()
    assert ctl.obstacle is True                            # swing readings don't finish the count


def test_front_servo_refused_while_forward_or_auto(client, ctl):
    post(client, "/api/drive", {"cmd": "forward"})
    r = post(client, "/api/front_servo", {"angle": config.FRONT_LEFT})
    assert r.status_code == 409 and "forward" in r.get_json()["error"]
    post(client, "/api/mode", {"mode": "avoid"})
    assert post(client, "/api/front_servo", {"angle": config.FRONT_LEFT}).status_code == 409
    post(client, "/api/drive", {"cmd": "stop"})
    assert post(client, "/api/front_servo", {"angle": config.FRONT_LEFT}).status_code == 200


def test_forward_reply_says_recentred(client, robot):
    post(client, "/api/front_servo", {"angle": config.FRONT_RIGHT})
    r = post(client, "/api/drive", {"cmd": "forward"})
    assert r.get_json()["recentred"] is True
    assert robot.front == config.FRONT_CENTER
    assert "recentred" not in post(client, "/api/drive", {"cmd": "forward"}).get_json()


# ---------- scan ----------

def test_median_cm():
    assert shared.median_cm([30.0, 500.0, 31.0]) == 31.0   # one bad echo can't win
    assert shared.median_cm([None, 40.0, 42.0]) == 41.0
    assert shared.median_cm([None, None, None]) is None


def test_scan_api(client, ctl, robot, clock):
    table = {a: 20.0 + i * 10 for i, a in enumerate(config.SCAN_ANGLES)}
    set_distances(robot, table)
    t0 = clock.t
    r = post(client, "/api/scan")
    assert r.status_code == 200
    data = r.get_json()
    assert data["angles"] == list(config.SCAN_ANGLES)
    assert data["distances_cm"] == [table[a] for a in config.SCAN_ANGLES]
    assert robot.front == config.FRONT_CENTER              # re-centred at the end
    assert ctl.scanning is False
    # waited SERVO_SETTLE_S at every angle + the final re-centre
    assert clock.t - t0 >= (len(config.SCAN_ANGLES) + 1) * config.SERVO_SETTLE_S


def test_scan_takes_median_of_3(client, robot):
    readings = iter([30.0, 400.0, 31.0] * len(config.SCAN_ANGLES))
    robot.read_distance_cm = lambda: next(readings)
    data = post(client, "/api/scan").get_json()
    assert data["distances_cm"] == [31.0] * len(config.SCAN_ANGLES)


def test_scan_refused_while_driving_or_auto(client):
    post(client, "/api/drive", {"cmd": "back"})
    r = post(client, "/api/scan")
    assert r.status_code == 409 and "stop" in r.get_json()["error"]
    post(client, "/api/mode", {"mode": "avoid"})
    assert post(client, "/api/scan").status_code == 409


def test_drive_refused_during_scan_but_stop_allowed(client, ctl):
    ctl.scanning = True
    r = post(client, "/api/drive", {"cmd": "back"})
    assert r.status_code == 409 and "scan" in r.get_json()["error"]
    assert post(client, "/api/drive", {"cmd": "stop"}).status_code == 200


# ---------- avoid ----------

def run_until(ctl, clock, state, max_s=10.0):
    start = clock.t
    while ctl.auto is not None and ctl.auto.state != state:
        step(ctl, clock)
        assert clock.t - start < max_s, "never reached %s" % state
    return clock.t - start


@pytest.mark.parametrize("left_cm, right_cm, expected", [
    (100.0, 30.0, "spin_left"),
    (30.0, 100.0, "spin_right"),
    (None, 60.0, "spin_right"),      # no echo counts as blocked
])
def test_avoid_picks_clearer_side(client, ctl, robot, clock, left_cm, right_cm, expected):
    table = {config.FRONT_CENTER: 100.0, config.FRONT_LEFT: left_cm, config.FRONT_RIGHT: right_cm}
    set_distances(robot, table)
    cmds = record_drives(robot)
    post(client, "/api/mode", {"mode": "avoid"})
    step(ctl, clock, 2)
    assert robot.last_cmd == "forward" and robot.speed == config.AVOID_SPEED

    table[config.FRONT_CENTER] = 20.0                      # something at 20 cm (< 25)
    run_until(ctl, clock, "spin")
    assert ctl.auto.choice == expected
    assert robot.last_cmd == expected
    assert "back" in cmds                                  # backed up before looking
    assert robot.front == config.FRONT_CENTER              # scan re-centred the sensor

    table[config.FRONT_CENTER] = 100.0                     # turned away: path clear
    run_until(ctl, clock, "drive")
    step(ctl, clock)
    assert robot.last_cmd == "forward"
    assert ctl.mode == "avoid"


def test_avoid_both_sides_blocked_turns_around(client, ctl, robot, clock):
    table = {config.FRONT_CENTER: 20.0, config.FRONT_LEFT: 10.0, config.FRONT_RIGHT: None}
    set_distances(robot, table)
    post(client, "/api/mode", {"mode": "avoid"})
    run_until(ctl, clock, "spin")
    assert ctl.auto.choice == "uturn"
    table[config.FRONT_CENTER] = 100.0                     # clear at once, but keep turning
    spun = run_until(ctl, clock, "drive")
    assert spun >= config.AVOID_UTURN_S


def test_avoid_no_way_out(client, ctl, robot, clock):
    set_distances(robot, {config.FRONT_CENTER: 20.0, config.FRONT_LEFT: 60.0,
                          config.FRONT_RIGHT: 30.0})
    post(client, "/api/mode", {"mode": "avoid"})
    run_until(ctl, clock, "spin")
    step(ctl, clock, int(config.AVOID_SPIN_MAX_S / config.AUTO_PERIOD_S) + 2)
    assert ctl.mode == "manual" and ctl.mode_reason == "no way out"
    assert robot.last_cmd == "stop"


def test_avoid_hard_obstacle_sets_flag_then_escapes(client, ctl, robot, clock):
    table = {config.FRONT_CENTER: 100.0, config.FRONT_LEFT: 80.0, config.FRONT_RIGHT: 30.0}
    set_distances(robot, table)
    post(client, "/api/mode", {"mode": "avoid"})
    step(ctl, clock, 2)
    table[config.FRONT_CENTER] = 10.0                      # sudden: under the 15 cm safety stop
    ctl.sensor_tick()
    assert robot.last_cmd == "stop" and ctl.obstacle is True
    ctl.auto_tick()
    assert ctl.auto.state == "back" and robot.last_cmd == "back"
    assert ctl.beeps                                       # safety stop beeped


# ---------- shutdown ----------

def test_shutdown_during_auto(client, ctl, robot, clock):
    post(client, "/api/mode", {"mode": "line_follow"})
    set_line(robot, CENTRED)
    step(ctl, clock)
    ctl.shutdown()
    assert robot.cleaned_up and robot.last_cmd == "stop"
    ctl.auto_tick()                                        # no crash, no motor call
    assert robot.last_cmd == "stop"
