"""FakeRobot: same interface as hardware.RealRobot, but only prints. For testing on Windows.
FakeDht11 + fake_connect_pigpio: stand-ins for hardware.Dht11 / connect_pigpio (--fake climate).
FakeSoil + fake_find_soil_ports: stand-ins for soil.open_serial / find_ports (--fake soil)."""

import random
import time

import config
import shared


class FakeRobot(object):

    def __init__(self):
        self.last_cmd = "stop"
        self.speed = 0
        self.pan = config.SERVO_CENTER
        self.tilt = config.SERVO_CENTER
        self.front = config.SERVO_CENTER
        self.led = (0, 0, 0)
        self.buzzing = False
        self.relaxed = set(shared.SERVO_BY_NAME)  # servos with pulses off (all at start)
        self.calls = []                           # method names in call order, for tests
        self.cleaned_up = False
        print("[FAKE] robot ready")

    # ---------- motors ----------

    def drive(self, cmd, speed=config.DEFAULT_SPEED):
        shared.check_command(cmd)
        speed = shared.clamp_speed(speed)
        if cmd == "stop":
            speed = 0
        self.last_cmd = cmd
        self.speed = speed
        self.calls.append("drive")
        print("[FAKE] drive %s %s" % (cmd, speed))
        return speed

    def stop(self):
        self.drive("stop", 0)

    # ---------- servos ----------

    def _moved(self, names, wait):
        # Like RealRobot: wait=True relaxes by itself, wait=False leaves pulses on.
        for name in names:
            if wait:
                self.relaxed.add(name)
            else:
                self.relaxed.discard(name)

    def set_camera(self, pan, tilt, wait=True):
        self.pan = shared.clamp_pan(pan)
        self.tilt = shared.clamp_tilt(tilt)
        self._moved(("pan", "tilt"), wait)
        self.calls.append("set_camera")
        print("[FAKE] camera pan=%s tilt=%s" % (self.pan, self.tilt))
        return self.pan, self.tilt

    def set_front_servo(self, angle, wait=True):
        self.front = shared.clamp_front(angle)
        self._moved(("front",), wait)
        self.calls.append("set_front_servo")
        print("[FAKE] front servo %s" % self.front)
        return self.front

    def relax_servo(self, name):
        shared.check_servo_name(name)
        self.relaxed.add(name)
        self.calls.append("relax_servo")
        print("[FAKE] relax %s" % name)

    # ---------- sensors ----------

    def read_distance_cm(self):
        # Always above OBSTACLE_CLEAR_CM so --fake runs never hit a random obstacle stop.
        # Tests replace this method to simulate an obstacle.
        return round(random.uniform(30, 200), 1)

    def read_line_sensors(self):
        return {"L1": False, "L2": False, "R1": False, "R2": False}

    def read_light_sensors(self):
        # UNUSED, like RealRobot's: LDRs not fitted on this build. Returns 0/0 like the real tank.
        return {"left": 0, "right": 0}

    # ---------- buzzer / LED ----------

    def buzzer(self, on):
        self.buzzing = bool(on)
        self.calls.append("buzzer")
        print("[FAKE] buzzer %s" % ("on" if on else "off"))

    def beep(self, seconds=0.2):
        seconds = shared.clamp_beep(seconds)
        print("[FAKE] beep %s s" % seconds)
        return seconds

    def set_led(self, r, g, b):
        self.led = (shared.clamp_color(r), shared.clamp_color(g), shared.clamp_color(b))
        print("[FAKE] led %s" % (self.led,))
        return self.led

    # ---------- shutdown ----------

    def cleanup(self):
        if self.cleaned_up:
            return
        self.stop()
        self.set_led(0, 0, 0)
        self.buzzing = False
        self.cleaned_up = True
        print("[FAKE] cleanup")


# ---------- DHT11 (for climate.ClimateMonitor with --fake) ----------

FAKE_CLIMATE = (24.0, 60.0)  # temp_c, humidity_pct: fixed, so tests know what to expect


class FakePigpio(object):
    """pigpio.pi stand-in: the monitor only ever calls stop() on it."""

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def fake_connect_pigpio():
    return FakePigpio(), None


class FakeDht11(object):

    def __init__(self, pi=None):
        self.closed = False
        print("[FAKE] DHT11 on")

    def read(self):
        return FAKE_CLIMATE

    def close(self):
        self.closed = True
        print("[FAKE] DHT11 off")


FAKE_SOIL_RAW = 600  # moist soil: 57 %, "ok"


def fake_find_soil_ports():
    return ["FAKE"]


class FakeSoil(object):
    """A fake Uno port: "READY" first, then "SOIL 600" every 2 s, like the real firmware."""

    def __init__(self, path=None, sleep=time.sleep):
        self.path = path
        self.sleep = sleep
        self.lines = 0
        self.closed = False
        print("[FAKE] soil Uno on %s" % path)

    def readline(self):
        self.sleep(2.0)
        self.lines += 1
        if self.lines == 1:
            return b"READY\r\n"
        return ("SOIL %d\r\n" % FAKE_SOIL_RAW).encode("ascii")

    def close(self):
        self.closed = True
