"""Pure logic shared by RealRobot and FakeRobot. No GPIO here, so it can be tested on Windows."""

import config

# (left_fwd, left_back, right_fwd, right_back) pin levels for each command.
# Copied from Yahboom's run/back/left/right/spin_left/spin_right/brake
# (reference/python/servo_ultrasonic_avoid.py:64-122).
DRIVE_TABLE = {
    "forward":    (1, 0, 1, 0),
    "back":       (0, 1, 0, 1),
    "left":       (0, 0, 1, 0),  # left track stopped, right track forward
    "right":      (1, 0, 0, 0),  # left track forward, right track stopped
    "spin_left":  (0, 1, 1, 0),  # tracks opposite ways: turn on the spot
    "spin_right": (1, 0, 0, 1),
    "stop":       (0, 0, 0, 0),
}


# Servo name -> BCM pin. Used by relax_servo(name) in both robot classes and by the server.
SERVO_BY_NAME = {
    "pan": config.SERVO_PAN,
    "tilt": config.SERVO_TILT,
    "front": config.SERVO_FRONT,
}


def check_servo_name(name):
    """Raise ValueError if name is not pan / tilt / front."""
    if name not in SERVO_BY_NAME:
        raise ValueError("unknown servo: %r (use one of %s)"
                         % (name, ", ".join(sorted(SERVO_BY_NAME))))


def clamp(value, lo, hi):
    """Force value into the range lo..hi."""
    return max(lo, min(hi, value))


def clamp_speed(speed):
    return clamp(speed, config.SPEED_MIN, config.SPEED_MAX)


def clamp_pan(angle):
    return clamp(angle, config.PAN_MIN, config.PAN_MAX)


def clamp_tilt(angle):
    return clamp(angle, config.TILT_MIN, config.TILT_MAX)


def clamp_front(angle):
    return clamp(angle, config.FRONT_MIN, config.FRONT_MAX)


def clamp_color(value):
    return clamp(value, config.COLOR_MIN, config.COLOR_MAX)


def clamp_beep(seconds):
    return clamp(seconds, 0.0, config.BEEP_MAX_S)


def angle_to_pulse_us(angle):
    """Servo angle 0-180 -> pulse width in microseconds (for pigpio)."""
    span = config.SERVO_PULSE_MAX_US - config.SERVO_PULSE_MIN_US
    return config.SERVO_PULSE_MIN_US + span * angle / 180.0


def angle_to_duty(angle):
    """Servo angle 0-180 -> duty cycle % at 50 Hz (for RPi.GPIO). CLAUDE.md formula."""
    return 2.5 + 10.0 * angle / 180.0


def color_to_duty(value):
    """LED brightness 0-255 -> duty cycle 0-100 (bluetooth_control.py:251-253)."""
    return 100.0 * value / 255.0


def check_command(cmd):
    """Raise ValueError if cmd is not a known drive command."""
    if cmd not in DRIVE_TABLE:
        raise ValueError("unknown drive command: %r (use one of %s)"
                         % (cmd, ", ".join(sorted(DRIVE_TABLE))))


# Commands that move the tank forward (at least one track). "left" / "right" drive one track
# forward, so in auto modes they count as moving forward for the obstacle stop.
FORWARD_MOVES = ("forward", "left", "right")


def line_command(s):
    """Line sensors {"L1","L2","R1","R2": True = black} -> (command, hold_s).

    Yahboom's rules from reference/python/tracking.py:146-183, checked in the same order.
    Physical order left -> right: L1 L2 R1 R2 (L2/R1 are the middle pair, over the tape
    when centred). hold_s > 0: keep this command that long (sharp corners).
    All white -> (None, 0): the caller keeps the previous command (Yahboom does the same).
    """
    l1, l2, r1, r2 = s["L1"], s["L2"], s["R1"], s["R2"]
    if (l1 or l2) and r2:
        return "spin_right", config.LINE_SHARP_HOLD_S  # sharp right corner (also all black)
    if l1 and (r1 or r2):
        return "spin_left", config.LINE_SHARP_HOLD_S   # sharp left corner
    if l1:
        return "spin_left", 0.0                        # line far left
    if r2:
        return "spin_right", 0.0                       # line far right
    if l2 and not r1:
        return "left", 0.0                             # drifted right: steer left
    if r1 and not l2:
        return "right", 0.0                            # drifted left: steer right
    if l2 and r1:
        return "forward", 0.0                          # centred
    return None, 0.0


def line_speed(cmd):
    """Speed for a line-follow command (Yahboom uses different speeds per move)."""
    if cmd == "forward":
        return config.LINE_SPEED
    if cmd in ("left", "right"):
        return config.LINE_TURN_SPEED
    return config.LINE_SPIN_SPEED


def median_cm(readings):
    """Median of the real readings (None = no echo, skipped). None if there is no real reading."""
    real = sorted(r for r in readings if r is not None)
    if not real:
        return None
    middle = len(real) // 2
    if len(real) % 2:
        return real[middle]
    return round((real[middle - 1] + real[middle]) / 2.0, 1)


DHT11_BITS = 40  # 5 bytes: humidity, humidity tenths, temperature, temperature tenths, checksum


def tick_diff(start, end):
    """Microseconds from tick start to tick end. pigpio ticks are 32 bit (wrap every ~72 min)."""
    return (end - start) & 0xFFFFFFFF


def decode_dht11(edges):
    """DHT11 answer -> (temperature_c, humidity_pct). Raises ValueError with the reason.

    edges: [(level, tick), ...] in the order pigpio's callback reported them (tick in us).
    The sensor sends each bit as ~50 us low, then high: ~27 us high = 0, ~70 us high = 1.
    Only the high times matter. The last 40 complete high pulses are the bits; the ones before
    are our own start signal and the sensor's 80 us "ready" pulse.
    """
    highs = []
    rise = None
    for level, tick in edges:
        if level == 1:
            rise = tick
        elif level == 0 and rise is not None:
            highs.append(tick_diff(rise, tick))
            rise = None
    if len(highs) < DHT11_BITS:
        raise ValueError("no answer (%d of %d bits): check wiring and power"
                         % (len(highs), DHT11_BITS))
    data = [0] * 5
    for i, high_us in enumerate(highs[-DHT11_BITS:]):
        data[i // 8] = (data[i // 8] << 1) | (1 if high_us > config.DHT11_ONE_US else 0)
    if sum(data[:4]) & 0xFF != data[4]:
        raise ValueError("checksum error")
    if not any(data):
        raise ValueError("all zeros: no real reading")
    humidity = data[0] + data[1] / 10.0
    temperature = data[2] + (data[3] & 0x7F) / 10.0
    if data[3] & 0x80:  # newer DHT11s: top bit of the tenths byte = below 0 C
        temperature = -temperature
    if not (0 < humidity <= 100 and -20 <= temperature <= 60):
        raise ValueError("out of range: %.1f C, %.1f %%RH" % (temperature, humidity))
    return temperature, humidity


def pick_side(left_cm, right_cm):
    """Where should avoid spin to? -> "spin_left", "spin_right" or "uturn".

    None (no echo) counts as blocked: the sensor also gives no echo when something is very close
    or at an angle, so it is not proof of open space. Both sides blocked -> turn around.
    """
    left = left_cm if left_cm is not None else 0.0
    right = right_cm if right_cm is not None else 0.0
    if left < config.AVOID_STOP_CM and right < config.AVOID_STOP_CM:
        return "uturn"
    return "spin_left" if left > right else "spin_right"
