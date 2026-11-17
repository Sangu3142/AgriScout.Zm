"""RealRobot: drives the real tank. Dht11: the temperature / humidity sensor (through pigpiod).
The ONLY file that imports RPi.GPIO / pigpio.

Must run on the Pi with Python 3.7.3.
"""

import subprocess
import time

import RPi.GPIO as GPIO

import config
import shared

SERVO_PINS = (config.SERVO_FRONT, config.SERVO_PAN, config.SERVO_TILT)
LINE_PINS = (("L1", config.LINE_L1), ("L2", config.LINE_L2),
             ("R1", config.LINE_R1), ("R2", config.LINE_R2))


def _pigpiod_running():
    """True if a process named exactly 'pigpiod' is running (pgrep exits 0 when it finds one)."""
    try:
        return subprocess.call(["pgrep", "-x", "pigpiod"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def connect_pigpio():
    """(pigpio.pi, None) connected to pigpiod on config.PIGPIO_PORT, or (None, reason).

    Used by the DHT11 (tools/dht_test.py) and by the servos, if config.SERVOS_USE_PIGPIO.
    pigpiod is not started at boot: `sudo pigpiod -l -p 8889` (CLAUDE.md).
    """
    # Check the daemon first: without it, pigpio connects to whatever owns the port.
    # Its default 8888 is Jupyter Lab, which never replies, so the connect hung forever.
    if not _pigpiod_running():
        return None, "pigpiod is not running"
    try:
        import pigpio
    except ImportError:
        return None, "pigpio module not installed"
    pi = pigpio.pi("localhost", config.PIGPIO_PORT)
    if not pi.connected:
        return None, "pigpiod not reachable on port %d" % config.PIGPIO_PORT
    return pi, None


class RealRobot(object):

    def __init__(self):
        # BCM 0/1/7/8/9/11 are also I2C/SPI pins and print harmless
        # "channel already in use" warnings; Yahboom silences them too.
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        self._closed = False

        # Motors: direction pins start LOW (= stopped), speed PWM starts at 0 %.
        for pin in (config.LEFT_FWD, config.LEFT_BACK, config.RIGHT_FWD, config.RIGHT_BACK):
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(config.LEFT_PWM, GPIO.OUT)
        GPIO.setup(config.RIGHT_PWM, GPIO.OUT)
        self._pwm_left = GPIO.PWM(config.LEFT_PWM, config.MOTOR_PWM_HZ)
        self._pwm_right = GPIO.PWM(config.RIGHT_PWM, config.MOTOR_PWM_HZ)
        self._pwm_left.start(0)
        self._pwm_right.start(0)

        # Sensors.
        GPIO.setup(config.ULTRASONIC_TRIG, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(config.ULTRASONIC_ECHO, GPIO.IN)
        for _, pin in LINE_PINS:
            GPIO.setup(pin, GPIO.IN)
        GPIO.setup(config.LDR_LEFT, GPIO.IN)
        GPIO.setup(config.LDR_RIGHT, GPIO.IN)

        # Buzzer is active-low: HIGH = silent.
        GPIO.setup(config.BUZZER, GPIO.OUT, initial=GPIO.HIGH)

        # RGB LED, brightness by PWM.
        self._pwm_leds = []
        for pin in (config.LED_R, config.LED_G, config.LED_B):
            GPIO.setup(pin, GPIO.OUT)
            pwm = GPIO.PWM(pin, config.LED_PWM_HZ)
            pwm.start(0)
            self._pwm_leds.append(pwm)

        # Servos: RPi.GPIO PWM, tested on the tank (no jitter). pigpio's hardware-timed pulses only
        # if config.SERVOS_USE_PIGPIO: pigpiod may now be running just for the DHT11.
        self._pi = None
        if config.SERVOS_USE_PIGPIO:
            self._pi, reason = connect_pigpio()
            if self._pi is None:
                print("WARNING: %s, servos use RPi.GPIO PWM (may jitter)" % reason)
        self._pwm_servos = {}
        if self._pi is None:
            for pin in SERVO_PINS:
                GPIO.setup(pin, GPIO.OUT)
                pwm = GPIO.PWM(pin, config.SERVO_PWM_HZ)
                pwm.start(0)  # duty 0 = no pulses = servo relaxed
                self._pwm_servos[pin] = pwm

    # ---------- motors ----------

    def drive(self, cmd, speed=config.DEFAULT_SPEED):
        shared.check_command(cmd)
        speed = shared.clamp_speed(speed)
        if cmd == "stop":
            speed = 0
        left_fwd, left_back, right_fwd, right_back = shared.DRIVE_TABLE[cmd]
        GPIO.output(config.LEFT_FWD, left_fwd)
        GPIO.output(config.LEFT_BACK, left_back)
        GPIO.output(config.RIGHT_FWD, right_fwd)
        GPIO.output(config.RIGHT_BACK, right_back)
        self._pwm_left.ChangeDutyCycle(speed)
        self._pwm_right.ChangeDutyCycle(speed)
        return speed

    def stop(self):
        self.drive("stop", 0)

    # ---------- servos ----------

    def _move_servo(self, pin, angle, wait=True):
        """wait=False: only start the move. The caller must call relax_servo() later
        (the server does this with a timer, so it never sleeps while holding its lock)."""
        if self._pi is not None:
            self._pi.set_servo_pulsewidth(pin, int(shared.angle_to_pulse_us(angle)))
        else:
            pwm = self._pwm_servos[pin]
            pwm.ChangeDutyCycle(shared.angle_to_duty(angle))
            if wait:
                time.sleep(config.SERVO_SETTLE_S)  # let it reach the angle
                pwm.ChangeDutyCycle(0)              # then stop pulses so it doesn't jitter

    def set_camera(self, pan, tilt, wait=True):
        pan = shared.clamp_pan(pan)
        tilt = shared.clamp_tilt(tilt)
        self._move_servo(config.SERVO_PAN, pan, wait)
        self._move_servo(config.SERVO_TILT, tilt, wait)
        return pan, tilt

    def set_front_servo(self, angle, wait=True):
        angle = shared.clamp_front(angle)
        self._move_servo(config.SERVO_FRONT, angle, wait)
        return angle

    def relax_servo(self, name):
        """Stop pulses on one servo ("pan", "tilt" or "front") after a wait=False move."""
        shared.check_servo_name(name)
        if self._pi is None:  # pigpio holds the angle without jitter: nothing to do
            self._pwm_servos[shared.SERVO_BY_NAME[name]].ChangeDutyCycle(0)

    # ---------- sensors ----------

    def read_distance_cm(self):
        """One ultrasonic reading in cm, or None if no echo / out of range. Never hangs."""
        # 15 us trigger pulse (servo_ultrasonic_avoid.py:152-156).
        GPIO.output(config.ULTRASONIC_TRIG, GPIO.LOW)
        time.sleep(0.000002)
        GPIO.output(config.ULTRASONIC_TRIG, GPIO.HIGH)
        time.sleep(0.000015)
        GPIO.output(config.ULTRASONIC_TRIG, GPIO.LOW)

        # Wait for the echo to start (each loop gives up after the timeout).
        deadline = time.perf_counter() + config.ULTRASONIC_TIMEOUT_S
        while not GPIO.input(config.ULTRASONIC_ECHO):
            if time.perf_counter() > deadline:
                return None
        start = time.perf_counter()

        # Echo pin stays HIGH for the sound's round trip time.
        deadline = start + config.ULTRASONIC_TIMEOUT_S
        while GPIO.input(config.ULTRASONIC_ECHO):
            if time.perf_counter() > deadline:
                return None
        end = time.perf_counter()

        cm = (end - start) * config.SPEED_OF_SOUND_CM_S / 2  # /2: there and back
        if cm <= 0 or cm >= config.DISTANCE_MAX_CM:
            return None
        return round(cm, 1)

    def read_line_sensors(self):
        """True = black line under that sensor (pin reads LOW, tracking.py:133)."""
        result = {}
        for name, pin in LINE_PINS:
            result[name] = GPIO.input(pin) == GPIO.LOW
        return result

    def read_light_sensors(self):
        """Raw digital levels 0/1 from the two LDR modules.

        UNUSED: the LDRs are not fitted on this G1 build (always reads 0/0, CLAUDE.md).
        Kept so the interface is complete. Light level comes from camera photos (phase 5).
        """
        return {"left": int(GPIO.input(config.LDR_LEFT)),
                "right": int(GPIO.input(config.LDR_RIGHT))}

    # ---------- buzzer / LED ----------

    def buzzer(self, on):
        """Buzzer is active-low: LOW = sound, HIGH = silent."""
        GPIO.output(config.BUZZER, GPIO.LOW if on else GPIO.HIGH)

    def beep(self, seconds=0.2):
        seconds = shared.clamp_beep(seconds)
        self.buzzer(True)
        try:
            time.sleep(seconds)
        finally:
            self.buzzer(False)
        return seconds

    def set_led(self, r, g, b):
        values = (shared.clamp_color(r), shared.clamp_color(g), shared.clamp_color(b))
        for pwm, value in zip(self._pwm_leds, values):
            pwm.ChangeDutyCycle(shared.color_to_duty(value))
        return values

    # ---------- shutdown ----------

    def cleanup(self):
        """Stop everything and release the pins. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        try:
            self.stop()
            self.set_led(0, 0, 0)
            GPIO.output(config.BUZZER, GPIO.HIGH)
            if self._pi is not None:
                for pin in SERVO_PINS:
                    self._pi.set_servo_pulsewidth(pin, 0)  # 0 = pulses off
                self._pi.stop()
            for pwm in list(self._pwm_servos.values()) + self._pwm_leds:
                pwm.stop()
            self._pwm_left.stop()
            self._pwm_right.stop()
        finally:
            GPIO.cleanup()


class Dht11(object):
    """DHT11 temperature / humidity sensor on one GPIO, read through pigpiod.

    The DHT11 sends its bits as high pulses of ~27 us (0) or ~70 us (1): too short for Python to
    time, especially next to the motor PWM threads. pigpiod samples the pins every 5 us and
    timestamps each edge itself; our callback only collects (level, tick), and
    shared.decode_dht11() turns them into numbers.
    Independent of RealRobot: it needs only a pigpio connection (connect_pigpio()) and its pin,
    so it can run next to the server. Read at most every 1-2 s (DHT11 datasheet).
    power_pin: GPIO that powers the module (HIGH = 3.3 V), switched on here and off in close();
    None = the module is on a fixed 3.3 V pin.
    """

    def __init__(self, pi, gpio=config.DHT11_PIN, power_pin=config.DHT11_POWER_PIN):
        import pigpio  # connect_pigpio() already imported it, so it is installed
        self._pigpio = pigpio
        self._pi = pi
        self._gpio = gpio
        self._power_pin = power_pin
        self._edges = []
        pi.set_mode(gpio, pigpio.INPUT)
        pi.set_pull_up_down(gpio, pigpio.PUD_OFF)  # the module has its own pull-up (to 3.3 V)
        self._callback = pi.callback(gpio, pigpio.EITHER_EDGE, self._on_edge)
        if power_pin is not None:
            pi.set_mode(power_pin, pigpio.OUTPUT)
            pi.write(power_pin, 1)                       # 3.3 V to the module's VCC
            time.sleep(config.DHT11_POWER_ON_WAIT_S)     # it needs > 1 s before the first read

    def _on_edge(self, gpio, level, tick):
        self._edges.append((level, tick))  # runs in pigpio's thread; append is atomic

    def read(self):
        """(temperature_c, humidity_pct). Raises ValueError if this reading failed (try later)."""
        self._edges = []
        self._pi.set_mode(self._gpio, self._pigpio.OUTPUT)
        self._pi.write(self._gpio, 0)                       # start signal: DATA low...
        time.sleep(config.DHT11_START_LOW_S)                # ...for at least 18 ms
        self._pi.set_mode(self._gpio, self._pigpio.INPUT)   # release: the pull-up takes it high,
        time.sleep(config.DHT11_ANSWER_WAIT_S)              # then the sensor answers
        return shared.decode_dht11(list(self._edges))

    def close(self):
        """Stop listening; the pin stays an input. Power pin (if any): off, then back to input."""
        self._callback.cancel()
        self._pi.set_mode(self._gpio, self._pigpio.INPUT)
        if self._power_pin is not None:
            self._pi.write(self._power_pin, 0)
            self._pi.set_mode(self._power_pin, self._pigpio.INPUT)
