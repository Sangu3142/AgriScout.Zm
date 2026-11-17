"""Test one part of the tank at a time.

Usage (on the Pi):   python3 test_parts.py <part>
Usage (on Windows):  python robot/test_parts.py <part> --fake
Parts: motors servos ultrasonic line led buzzer
"""

import subprocess
import sys
import time

import config

# No "light" part: the LDR sensors are not fitted on this G1 build (CLAUDE.md).
PARTS = ("motors", "servos", "ultrasonic", "line", "led", "buzzer")


def test_motors(robot):
    print("Motors at %d%% in 3 s. Lift the tank or clear the space!" % config.TEST_SPEED)
    for n in (3, 2, 1):
        print(n)
        time.sleep(1)
    for cmd in ("forward", "back", "left", "right", "spin_left", "spin_right"):
        print(">>> " + cmd)
        robot.drive(cmd, config.TEST_SPEED)
        time.sleep(1)
        robot.stop()
        time.sleep(0.5)


def test_servos(robot):
    for name, pan in (("center", config.PAN_CENTER), ("RIGHT", config.PAN_RIGHT),
                      ("LEFT", config.PAN_LEFT), ("center", config.PAN_CENTER)):
        print("camera pan -> %d (looks %s)" % (pan, name))
        robot.set_camera(pan, config.TILT_LEVEL)
        time.sleep(1)
    for name, tilt in (("level", config.TILT_LEVEL), ("DOWN", config.TILT_DOWN),
                       ("UP", config.TILT_UP), ("level", config.TILT_LEVEL)):
        print("camera tilt -> %d (looks %s)" % (tilt, name))
        robot.set_camera(config.PAN_CENTER, tilt)
        time.sleep(1)
    for name, angle in (("center", config.FRONT_CENTER), ("LEFT", config.FRONT_LEFT),
                        ("RIGHT", config.FRONT_RIGHT), ("center", config.FRONT_CENTER)):
        print("front servo -> %d (points %s)" % (angle, name))
        robot.set_front_servo(angle)
        time.sleep(1)


def test_ultrasonic(robot):
    print("Move your hand in front of the sensor.")
    for i in range(10):
        cm = robot.read_distance_cm()
        print("%2d: %s" % (i + 1, "no echo" if cm is None else "%.1f cm" % cm))
        time.sleep(0.5)


def test_line(robot):
    print("Hold black tape under the sensors. True = black line seen.")
    for _ in range(20):
        print(robot.read_line_sensors())
        time.sleep(0.5)


def test_led(robot):
    for name, rgb in (("red", (255, 0, 0)), ("green", (0, 255, 0)), ("blue", (0, 0, 255)),
                      ("white", (255, 255, 255)), ("off", (0, 0, 0))):
        print("LED " + name)
        robot.set_led(*rgb)
        time.sleep(1)


def test_buzzer(robot):
    for i in range(3):
        print("beep %d" % (i + 1))
        robot.beep(0.2)
        time.sleep(0.3)


def main():
    args = sys.argv[1:]
    fake = "--fake" in args
    args = [a for a in args if a != "--fake"]
    if len(args) != 1 or args[0] not in PARTS:
        print(__doc__)
        return 2
    part = args[0]
    # Printed before touching any hardware, so if it hangs we know it's in setup.
    print("Starting %s test..." % part, flush=True)

    if fake:
        from fake_hardware import FakeRobot
        robot = FakeRobot()
    else:
        # Yahboom's Bluetooth app drives the same pins; make it let go.
        subprocess.call(["pkill", "-f", "bluetooth_control_tank"])
        time.sleep(0.5)
        from hardware import RealRobot
        robot = RealRobot()

    try:
        globals()["test_" + part](robot)
        print("done")
    except KeyboardInterrupt:
        print("\nstopped by Ctrl+C")
    finally:
        robot.stop()
        robot.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
