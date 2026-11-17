"""All robot constants in one place.

Pin numbers use BCM numbering (GPIO.setmode(GPIO.BCM)).
Sources: CLAUDE.md pin map, cross-checked against reference/python/*.py (Yahboom).
"""

# ---- Motors (CLAUDE.md pin map; reference/python/servo_ultrasonic_avoid.py:6-11) ----
LEFT_FWD = 20    # IN1: HIGH + LEFT_BACK LOW = left track forward
LEFT_BACK = 21   # IN2
LEFT_PWM = 16    # ENA: speed of left motor
RIGHT_FWD = 19   # IN3
RIGHT_BACK = 26  # IN4
RIGHT_PWM = 13   # ENB: speed of right motor

MOTOR_PWM_HZ = 2000  # servo_ultrasonic_avoid.py:55 (GPIO.PWM(ENA, 2000))
SPEED_MIN = 0        # duty cycle %, RPi.GPIO range is 0-100 (CLAUDE.md)
SPEED_MAX = 100
DEFAULT_SPEED = 50   # our choice: middle of range; Yahboom drives at 45-55 (servo_ultrasonic_avoid.py:256-258)
TEST_SPEED = 40      # our choice: slow enough to watch safely (phase 1 spec)

# ---- Ultrasonic (servo_ultrasonic_avoid.py:17-18) ----
ULTRASONIC_TRIG = 1
ULTRASONIC_ECHO = 0
ULTRASONIC_TIMEOUT_S = 0.03  # servo_ultrasonic_avoid.py:163,170 (gives up after 30 ms)
DISTANCE_MAX_CM = 500        # servo_ultrasonic_avoid.py:185 (readings >= 500 treated as bad)
SPEED_OF_SOUND_CM_S = 34000  # servo_ultrasonic_avoid.py:176 (340 m/s * 100)

# ---- IR line sensors, left -> right (tracking.py:18-21) ----
LINE_L1 = 3
LINE_L2 = 5
LINE_R1 = 4
LINE_R2 = 18

# ---- Light sensors LDR (TCP_Control.py:89-90) ----
LDR_LEFT = 7
LDR_RIGHT = 6

# ---- Servos (TCP_Control.py:67-69) ----
SERVO_FRONT = 23  # turns the ultrasonic sensor
SERVO_PAN = 11    # camera left/right
SERVO_TILT = 9    # camera up/down

SERVO_PWM_HZ = 50        # TCP_Control.py:160 (GPIO.PWM(FrontServoPin, 50))
SERVO_CENTER = 90        # TCP_Control.py:42-44 (all servos start at 90)
PAN_MIN = 0              # TCP_Control.py:364
PAN_MAX = 180            # TCP_Control.py:353
TILT_MIN = 45            # TCP_Control.py:341 (Yahboom never tilts below 45)
TILT_MAX = 180           # TCP_Control.py:330
FRONT_MIN = 0            # TCP_Control.py:369-373
FRONT_MAX = 180

# Which way each angle points, MEASURED on the real tank (CLAUDE.md "Servo directions").
# Use these names instead of raw angles. Front is mirrored vs pan!
PAN_RIGHT = 0            # camera looks right
PAN_CENTER = 90
PAN_LEFT = 180           # camera looks left
TILT_DOWN = 45           # camera looks down (= TILT_MIN)
TILT_LEVEL = 90
TILT_UP = 180            # camera looks up (= TILT_MAX)
FRONT_LEFT = 0           # ultrasonic points left (Yahboom's TCP_Control.py:369 says 180; wrong on this build)
FRONT_CENTER = 90
FRONT_RIGHT = 180        # ultrasonic points right
# Pulse width for pigpio. Yahboom duty 2.5-12.5 % of a 20 ms period = 0.5-2.5 ms
# (CLAUDE.md: duty = 2.5 + 10*angle/180).
SERVO_PULSE_MIN_US = 500
SERVO_PULSE_MAX_US = 2500
PIGPIO_PORT = 8889       # pigpio's default 8888 is taken by Jupyter Lab (CLAUDE.md);
                         # pigpiod must be started with `-p 8889`
SERVO_SETTLE_S = 0.3     # RPi.GPIO fallback only: time to reach angle before duty 0
                         # (Yahboom sets duty 0 to stop jitter, TCP_Control.py:384-396).
                         # Tested on the tank: no jitter, so pigpiod is optional.
SERVOS_USE_PIGPIO = False  # pigpiod runs for the DHT11 (started at boot), but the servos stay
                           # on the RPi.GPIO path tested on the tank. True = pigpio (untested)

# ---- DHT11 temperature / humidity (our addition, not in Yahboom's kit) ----
# DATA on BCM 17 = Yahboom's right avoid-obstacle pin (TCP_Control.py:75, only ever an input there).
# AgriScout doesn't use it, Yahboom's boot app never touches it. It comes out on the board's
# "Right avoid obstacle / seek light" socket. VCC must be 3.3 V (DHT11_POWER_PIN or a 3.3 V pin):
# the module's pull-up goes to its VCC, and the Pi's GPIO must never see 5 V (CLAUDE.md "DHT11").
DHT11_PIN = 17
DHT11_START_LOW_S = 0.02    # start signal: hold DATA low at least 18 ms (DHT11 datasheet)
DHT11_ANSWER_WAIT_S = 0.05  # the answer (80 + 80 us, then 40 bits) takes about 5 ms
DHT11_ONE_US = 50           # a bit's high pulse: ~27 us = 0, ~70 us = 1
# VCC from BCM 12 driven HIGH (3.3 V GPIO). Never the board's MVCC: it measured 5 V, and the Pi's
# GPIO must never see 5 V. BCM 12 = Yahboom's left avoid-obstacle pin: unused by AgriScout and by
# Yahboom's boot app. The DHT11 draws ~2.5 mA at most, well within what a GPIO can supply.
# None = fixed 3.3 V supply (STM32 header 3.3V pin measured 0 V on this board, not usable).
DHT11_POWER_PIN = 12
DHT11_POWER_ON_WAIT_S = 2.0  # after power-on the DHT11 needs > 1 s to stabilise (datasheet)
# The robot server reads it in the background (climate.ClimateMonitor), through pigpiod (started at
# boot by systemd: drop-in /etc/systemd/system/pigpiod.service.d/zz-agriscout.conf runs
# /usr/bin/pigpiod -l -p 8889). False = no monitor, /api/status reports the climate as turned off.
CLIMATE_ENABLED = True
CLIMATE_READ_PERIOD_S = 5.0  # our choice: room climate changes slowly; datasheet minimum is 1-2 s
CLIMATE_RETRY_S = 30.0       # pigpiod missing / lost: try again this often

# ---- Soil moisture (our addition): Arduino Uno on the Pi's USB, never on the GPIO ----
# The Uno reads the probe (VCC 5V / GND / AO -> A0 on the Uno) and sends "SOIL <raw 0-1023>" every
# 2 s ("READY" at start). Read only: nothing is ever sent to it. Opening the port resets the Uno,
# so the first line comes ~2 s later. Unplugged = "Uno not connected", the server keeps running.
SOIL_ENABLED = True
SOIL_PORT = "/dev/ttyUSB0"   # CH340 USB serial; if missing, soil.find_ports tries ttyUSB* / ttyACM*
SOIL_BAUD = 9600
# Calibration (measured): air 1023, dry soil 1000, moist 700, wet mud 300. Higher raw = drier.
SOIL_RAW_DRY = 1000          # 0 % moisture
SOIL_RAW_WET = 300           # 100 % moisture
SOIL_DRY_ABOVE = 850         # raw > this = "dry"
SOIL_WET_BELOW = 500         # raw < this = "wet"; in between = "ok"
SOIL_RETRY_S = 10.0          # Uno missing / unplugged: look for it again this often
SOIL_STALE_S = 10.0          # no line for this long = stale (the Uno sends one every 2 s)
SOIL_READ_TIMEOUT_S = 1.0    # serial readline timeout, so close() never waits long

# ---- Buzzer (bluetooth_control.py:47,100,238-241) ----
BUZZER = 8        # same pin as the start key (CLAUDE.md), active-low: LOW = sound
BEEP_MAX_S = 2.0  # our choice: a longer beep blocks the caller too long

# ---- RGB LED (bluetooth_control.py:120-122) ----
LED_R = 22
LED_G = 27
LED_B = 24
LED_PWM_HZ = 1000  # bluetooth_control.py:120
COLOR_MIN = 0      # bluetooth_control.py:251 (input 0-255, rescaled to duty 0-100)
COLOR_MAX = 255

# ---- Safety ----
WATCHDOG_TIMEOUT_S = 0.8  # CLAUDE.md: motors stop if no drive command within 0.8 s. Was 0.5 in phase 2,
                          # but requests over the robot Wi-Fi sometimes take 0.6-0.7 s (phase 3)
WATCHDOG_PERIOD_S = 0.05  # watchdog checks 20x/s, so a stop happens at most ~0.85 s after the last command
SENSOR_PERIOD_S = 0.1     # phase 2 spec: read the ultrasonic every 100 ms
OBSTACLE_STOP_CM = 15     # phase 2 spec: driving forward and closer than this -> stop
OBSTACLE_CLEAR_CM = 20    # our choice: flag clears only above 20 cm, so it doesn't flicker around 15
OBSTACLE_CLEAR_COUNT = 5  # ...and only after 5 such readings in a row (~0.5 s). Tank test: ONE false
                          # 51 cm reading with a hand at 4 cm cleared the flag (the Phase 2 bug).
                          # Was 3; run 1 showed 3 was too weak while the hand stayed put
BLIND_STOP_TICKS = 3      # this many "no echo" readings in a row while forward...
BLIND_STOP_NEAR_CM = 40   # ...stop, but only if the last real reading was closer than this
                          # (no echo after a far reading = open space, keep driving)
OBSTACLE_BEEP_S = 0.1     # short warning beep on an obstacle stop
FRONT_CENTER_TOLERANCE_DEG = 15  # forward re-centres the ultrasonic servo if it is further off than this

# ---- Auto modes (phase 4a) ----
MODES = ("manual", "line_follow", "avoid")
AUTO_PERIOD_S = 0.05      # auto loop runs at 20 Hz (phase 4a spec)
AUTO_HEARTBEAT_S = 3.0    # auto stops if no /api/status or /api/mode for this long (laptop gone)

# Line follow. Speeds from Yahboom's tracking.py:147-183 (spin 35, one-track turn 40, straight 50),
# except straight: 35 instead of 50, to start slow (phase 4a spec).
LINE_SPEED = 35
LINE_TURN_SPEED = 40
LINE_SPIN_SPEED = 35
LINE_SHARP_HOLD_S = 0.08  # tracking.py:148,158: keep spinning 80 ms on a sharp corner
LINE_LOST_S = 1.0         # all 4 sensors white this long -> line lost (phase 4a spec)

# Avoid
AVOID_SPEED = 35          # our choice: slow, so the 10 Hz sensor has time to see things
AVOID_STOP_CM = 25        # phase 4a spec: closer than this -> start the escape
AVOID_CLEAR_CM = 35       # phase 4a spec: spin until the path ahead is further than this...
AVOID_CLEAR_READINGS = 2  # ...twice in a row
AVOID_BACK_S = 0.4        # phase 4a spec: back up briefly
AVOID_SPIN_SPEED = 50     # our choice: tracks slip on the spot, a spin needs more power than driving
AVOID_SPIN_MIN_S = 0.3    # our choice: turn at least a bit, so we don't drive straight back in
AVOID_UTURN_S = 1.0       # both sides blocked: spin about 180 degrees. TUNE ON THE FLOOR
AVOID_SPIN_MAX_S = 4.0    # still no way out after this long -> stop, manual ("no way out")

# Scan: angles left -> right, built from the measured direction names (front is mirrored vs pan,
# so never write raw numbers here). Middle angles are halfway between.
SCAN_ANGLES = (FRONT_LEFT, (FRONT_LEFT + FRONT_CENTER) // 2, FRONT_CENTER,
               (FRONT_CENTER + FRONT_RIGHT) // 2, FRONT_RIGHT)
SCAN_READINGS = 3         # phase 4a spec: median of 3 readings per angle (one bad echo can't win)

# ---- Status LED colours (phase 4a spec), (r, g, b) 0-255 ----
LED_MANUAL = (0, 255, 0)       # green: manual
LED_AUTO = (0, 0, 255)         # blue: auto running
LED_OBSTACLE = (255, 0, 0)     # red, flashing: obstacle stop
LED_LINE_LOST = (255, 255, 0)  # yellow: line lost
LED_OFF = (0, 0, 0)
LED_FLASH_S = 0.25             # red flash: 0.25 s on, 0.25 s off

# ---- Start at boot (phase 4b, agriscout.service runs robot_server.py --boot) ----
# Yahboom's app is started by the desktop session a few seconds after boot (autostart), not by
# systemd, so the service can't simply be ordered after it. --boot waits for it, then kills it.
BLUETOOTH_APP = "bluetooth_control_tank"
BOOT_WAIT_S = 60          # wait at most this long for Yahboom's app to appear (boot takes ~15 s)
BOOT_POLL_S = 1.0         # look for it once a second
BOOT_GUARD_S = 180        # after start: keep killing it if it still appears late (slow desktop)...
BOOT_GUARD_PERIOD_S = 5   # ...checking every 5 s

# ---- Server (CLAUDE.md "Access") ----
SERVER_PORT = 5000
CAMERA_STREAM_PORT = 8080  # mjpg-streamer, started by /etc/rc.local
