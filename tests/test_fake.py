import ast
import glob
import os
import re

import pytest

import config
import shared
from conftest import ROBOT_DIR
from fake_hardware import FakeRobot


# ---------- clamping ----------

def test_clamp_speed():
    assert shared.clamp_speed(-5) == 0
    assert shared.clamp_speed(150) == 100
    assert shared.clamp_speed(40) == 40


def test_clamp_angles():
    assert shared.clamp_pan(-10) == 0
    assert shared.clamp_pan(200) == 180
    assert shared.clamp_tilt(0) == config.TILT_MIN == 45
    assert shared.clamp_front(90) == 90


def test_clamp_color_and_beep():
    assert shared.clamp_color(300) == 255
    assert shared.clamp_color(-1) == 0
    assert shared.clamp_beep(10) == config.BEEP_MAX_S
    assert shared.clamp_beep(-1) == 0


def test_servo_conversions():
    assert shared.angle_to_pulse_us(0) == 500
    assert shared.angle_to_pulse_us(90) == 1500
    assert shared.angle_to_pulse_us(180) == 2500
    assert shared.angle_to_duty(90) == 7.5
    assert shared.color_to_duty(255) == 100


def test_servo_direction_names():
    # Measured on the real tank (CLAUDE.md). Front is mirrored vs pan.
    assert (config.PAN_RIGHT, config.PAN_LEFT) == (0, 180)
    assert (config.TILT_DOWN, config.TILT_UP) == (45, 180)
    assert (config.FRONT_LEFT, config.FRONT_RIGHT) == (0, 180)
    # Every named angle must survive its own clamp unchanged.
    for a in (config.PAN_RIGHT, config.PAN_CENTER, config.PAN_LEFT):
        assert shared.clamp_pan(a) == a
    for a in (config.TILT_DOWN, config.TILT_LEVEL, config.TILT_UP):
        assert shared.clamp_tilt(a) == a
    for a in (config.FRONT_LEFT, config.FRONT_CENTER, config.FRONT_RIGHT):
        assert shared.clamp_front(a) == a


# ---------- drive table ----------

def test_drive_table_commands():
    expected = {"forward", "back", "left", "right", "spin_left", "spin_right", "stop"}
    assert set(shared.DRIVE_TABLE) == expected


def test_drive_table_values():
    assert shared.DRIVE_TABLE["forward"] == (1, 0, 1, 0)  # CLAUDE.md: FWD HIGH + BACK LOW
    assert shared.DRIVE_TABLE["stop"] == (0, 0, 0, 0)


def test_no_motor_driven_both_ways():
    for cmd, (lf, lb, rf, rb) in shared.DRIVE_TABLE.items():
        assert not (lf and lb), cmd
        assert not (rf and rb), cmd


def test_check_command_rejects_unknown():
    with pytest.raises(ValueError):
        shared.check_command("jump")


# ---------- FakeRobot ----------

@pytest.fixture
def robot():
    r = FakeRobot()
    yield r
    r.cleanup()


def test_fake_bad_command(robot):
    with pytest.raises(ValueError):
        robot.drive("fly", 50)


def test_fake_drive_clamps_speed(robot):
    robot.drive("forward", 250)
    assert robot.last_cmd == "forward"
    assert robot.speed == 100


def test_fake_stop_zero_speed(robot):
    robot.drive("forward", 50)
    robot.stop()
    assert robot.last_cmd == "stop"
    assert robot.speed == 0


def test_fake_camera_clamps(robot):
    assert robot.set_camera(-50, 10) == (0, 45)
    assert robot.set_front_servo(999) == 180


def test_fake_servo_wait_and_relax(robot):
    robot.set_camera(30, 90, wait=False)
    assert "pan" not in robot.relaxed and "tilt" not in robot.relaxed
    robot.relax_servo("pan")
    assert "pan" in robot.relaxed and "tilt" not in robot.relaxed
    robot.set_front_servo(20)  # wait=True relaxes by itself, like RealRobot
    assert "front" in robot.relaxed


def test_fake_relax_bad_name(robot):
    with pytest.raises(ValueError):
        robot.relax_servo("wheel")


def test_fake_buzzer(robot):
    robot.buzzer(True)
    assert robot.buzzing
    robot.buzzer(False)
    assert not robot.buzzing


def test_fake_led_clamps(robot):
    assert robot.set_led(300, -5, 128) == (255, 0, 128)


def test_fake_sensors(robot):
    cm = robot.read_distance_cm()
    assert isinstance(cm, float) and 0 < cm < config.DISTANCE_MAX_CM
    line = robot.read_line_sensors()
    assert set(line) == {"L1", "L2", "R1", "R2"}
    assert all(isinstance(v, bool) for v in line.values())
    light = robot.read_light_sensors()
    assert set(light) == {"left", "right"}
    assert all(v in (0, 1) for v in light.values())


def test_fake_cleanup_stops(robot):
    robot.drive("spin_left", 60)
    robot.cleanup()
    robot.cleanup()  # safe twice
    assert robot.last_cmd == "stop"
    assert robot.cleaned_up


def test_fake_has_same_methods_as_real():
    # Read hardware.py as text: importing it needs RPi.GPIO, which Windows doesn't have.
    with open(os.path.join(ROBOT_DIR, "hardware.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    real = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RealRobot")
    real_methods = {n.name for n in real.body
                    if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    for name in real_methods:
        assert callable(getattr(FakeRobot, name, None)), name


def test_pigpio_avoids_jupyter_port():
    # pigpio.pi() with no port connects to 8888 = Jupyter Lab and hangs (CLAUDE.md).
    assert config.PIGPIO_PORT != 8888
    with open(os.path.join(ROBOT_DIR, "hardware.py"), encoding="utf-8") as f:
        src = f.read()
    # Look at real calls only (ast), not comments/docstrings that mention pigpio.pi().
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "pi" and getattr(n.func.value, "id", None) == "pigpio"]
    assert calls, "hardware.py should call pigpio.pi(...)"
    for call in calls:
        args = [ast.unparse(a) for a in call.args + [k.value for k in call.keywords]]
        assert "config.PIGPIO_PORT" in args, "pigpio.pi() must pass config.PIGPIO_PORT"
    assert "pgrep" in src


def test_light_sensors_not_used():
    # LDRs are not fitted on this G1 build (CLAUDE.md): only the two robot classes may mention it.
    allowed = {"hardware.py", "fake_hardware.py"}
    for path in glob.glob(os.path.join(ROBOT_DIR, "**", "*.py"), recursive=True):
        if os.path.basename(path) in allowed:
            continue
        with open(path, encoding="utf-8") as f:
            assert "read_light_sensors" not in f.read(), path


# ---------- Python 3.7 rules for robot/ ----------

ROBOT_FILES = sorted(glob.glob(os.path.join(ROBOT_DIR, "*.py"))
                     + glob.glob(os.path.join(ROBOT_DIR, "tools", "*.py")))  # also deployed
BANNED = [
    (re.compile(r"\bremoveprefix\b|\bremovesuffix\b"), "str.removeprefix/removesuffix (3.9+)"),
    (re.compile(r"\bcached_property\b"), "functools.cached_property (3.8+)"),
    (re.compile(r"\b(list|dict|tuple|set)\["), "builtin generics like list[int] (3.9+)"),
    (re.compile(r"@app\.(get|post|put|delete)\b"), "Flask 2 route shortcuts (Pi has Flask 1.0.2)"),
]


@pytest.mark.parametrize("path", ROBOT_FILES, ids=os.path.basename)
def test_robot_file_is_python37(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    ast.parse(src, filename=path, feature_version=(3, 7))  # rejects :=, match, / params
    for pattern, why in BANNED:
        assert not pattern.search(src), "%s uses %s" % (os.path.basename(path), why)
