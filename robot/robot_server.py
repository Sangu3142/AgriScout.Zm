"""AgriScout robot server: one Flask app on port 5000 that owns all the hardware.

Runs on the Pi with Python 3.7.3 + Flask 1.0.2, so:
  - routes use @app.route(..., methods=[...]) (the get/post shortcuts are Flask 2 only)
  - views return jsonify(...), never a plain dict (dict returns need Flask 1.1)

Usage:
  python3 robot_server.py            real tank (stops Yahboom's bluetooth app first)
  python robot\\robot_server.py --fake   FakeRobot, for testing on Windows
"""

import argparse
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

from flask import Flask, jsonify, request

import auto
import climate
import config
import shared
import soil
from fake_hardware import (FakeDht11, FakeRobot, FakeSoil, fake_connect_pigpio,
                           fake_find_soil_ports)


class ApiError(Exception):
    """Raised anywhere in a request -> JSON reply {"ok": false, "error": message} with this status."""

    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def start_timer(delay, fn, *args):
    """Run fn(*args) once after delay seconds, in a background thread."""
    timer = threading.Timer(delay, fn, args)
    timer.daemon = True  # don't keep the program alive at exit
    timer.start()


class RobotController(object):
    """All robot state + every hardware call. The Flask routes only validate input and call this.

    Locking rule: every robot.* call happens inside `with self.hw_lock:`, so two threads never
    talk to GPIO at the same time. The lock is only held for the GPIO call itself; waits
    (beep length, servo settle time) happen outside it, so the safety threads never wait long.
    """

    def __init__(self, robot, climate_monitor=None, soil_monitor=None):
        self.robot = robot
        # DHT11 reader (climate.ClimateMonitor). Its own thread and locks: the sensor pins are
        # pigpio pins, never touched under hw_lock. None = turned off.
        self.climate = climate_monitor if climate_monitor is not None else climate.DisabledClimate()
        # Soil moisture via the Uno on USB serial (soil.SoilMonitor): own thread and locks too,
        # no GPIO at all. Information only. None = turned off.
        self.soil = soil_monitor if soil_monitor is not None else soil.DisabledSoil()
        self.hw_lock = threading.Lock()
        self.beep_lock = threading.Lock()  # one beep at a time (NOT the hardware lock)
        self.closed = False

        # Hooks that tests replace to control time.
        self.schedule = start_timer
        self.sleep = time.sleep
        self.clock = time.monotonic

        self.cmd = "stop"
        self.speed = 0
        self.pan = config.PAN_CENTER
        self.tilt = config.TILT_LEVEL
        self.front = config.FRONT_CENTER
        self.distance_cm = None
        self.line = None
        self.obstacle = False
        self.clear_count = 0      # real readings > OBSTACLE_CLEAR_CM in a row
        self.none_count = 0       # "no echo" readings in a row
        self.last_real_cm = None  # last reading that was not None
        self.blind_stopped = False  # last stop was a blind stop; cleared by a real reading ahead
        self.reading_seq = 0        # +1 per sensor reading, so auto can tell new readings from old

        # Modes (phase 4a). self.auto is the running LineFollower/Avoider, None in manual.
        self.mode = "manual"
        self.auto = None
        self.mode_reason = ""       # why auto last ended ("line lost", "heartbeat lost", ...)
        self.scanning = False

        # LED: what is shown now, the /api/led colour (until the mode changes), line-lost flag.
        self.led = None
        self.led_override = None
        self.line_lost = False

        self.start_time = self.clock()
        self.last_drive_time = self.start_time
        self.last_heartbeat = self.start_time

        # How many times each servo was moved. A delayed relax only runs if no newer move happened.
        self.servo_moves = {"pan": 0, "tilt": 0, "front": 0}

        self._stop_event = threading.Event()
        self._threads = []
        self._beep_thread = None

    def _check_open(self):
        # Call with hw_lock held.
        if self.closed:
            raise ApiError(503, "robot is shutting down")

    # ---------- motors ----------

    def drive(self, cmd, speed):
        """A MANUAL drive command (from /api/drive). Returns (speed, recentred).

        Any manual command, stop included, ends a running auto mode first: the user always wins.
        """
        with self.hw_lock:
            self._check_open()
            if self.auto is not None:
                self._stop_motors()
                self._set_mode("manual", "manual command")
                print("AUTO cancelled by manual %s" % cmd)
            self.line_lost = False
            if self.scanning and cmd != "stop":  # stop is always allowed
                raise ApiError(409, "scan in progress: wait for it to finish")
            blocked = cmd == "forward" and self.obstacle
        if blocked:
            raise self._blocked_error()

        recentred = False
        if cmd == "forward" and not self.front_is_ahead():
            # The ultrasonic sits on the front servo. Point it where we are going
            # before the motors start, so the obstacle stop looks ahead.
            self.set_front_servo(config.FRONT_CENTER)
            self.sleep(config.SERVO_SETTLE_S)
            recentred = True

        refused = False
        with self.hw_lock:
            self._check_open()
            if cmd == "forward":
                if self.obstacle:  # may have changed while re-centring
                    raise self._blocked_error()
                # Fresh reading before the motors start, so we don't drive into
                # something that is already there and wait for the next sensor tick.
                distance = self.robot.read_distance_cm()
                self._note_reading(distance)
                refused = distance is not None and distance < config.OBSTACLE_STOP_CM
                if refused:
                    self._set_obstacle()
            if not refused:
                speed = self.robot.drive(cmd, speed)
                self.cmd = cmd
                self.speed = speed
                self.last_drive_time = self.clock()
        if refused:
            print("OBSTACLE at %.1f cm: forward refused" % distance)
            self.beep(config.OBSTACLE_BEEP_S)
            raise self._blocked_error()
        return speed, recentred

    def auto_drive(self, owner, cmd, speed):
        """Drive for an auto mode. Returns False (and does nothing) if `owner` is no longer the
        running auto (a manual command cancelled it meanwhile), or if a move that goes forward
        is asked while the obstacle flag or a blind stop is active."""
        with self.hw_lock:
            if self.closed or self.auto is not owner:
                return False
            if cmd in shared.FORWARD_MOVES and (self.obstacle or self.blind_stopped):
                return False
            if cmd == "stop":
                speed = 0
            if cmd == self.cmd and speed == self.speed:
                return True  # already doing it: don't touch the GPIO 20x/s for nothing
            self.speed = self.robot.drive(cmd, speed)
            self.cmd = cmd
            self.last_drive_time = self.clock()
            return True

    def _blocked_error(self):
        return ApiError(409, "obstacle ahead (< %d cm): forward blocked, back away or turn"
                        % config.OBSTACLE_STOP_CM)

    def _stop_motors(self):
        # Call with hw_lock held.
        self.robot.stop()
        self.cmd = "stop"
        self.speed = 0

    # ---------- servos ----------

    def _bump(self, names):
        """Count a new move for each servo. Returns the new counts (the 'ticket'). hw_lock held."""
        ticket = {}
        for name in names:
            self.servo_moves[name] += 1
            ticket[name] = self.servo_moves[name]
        return ticket

    def _relax_if_latest(self, ticket):
        """Timer callback: relax each servo in the ticket, unless it was moved again since.

        Example: move A (pan #1), move B (pan #2) 0.1 s later. A's timer fires: 1 != 2, skip.
        B's timer fires: 2 == 2, relax. So an old timer never cuts a newer move short.
        Check and relax happen under the same lock, so no move can sneak in between.
        """
        with self.hw_lock:
            if self.closed:
                return
            for name in sorted(ticket):
                if self.servo_moves[name] == ticket[name]:
                    self.robot.relax_servo(name)

    def set_camera(self, pan=None, tilt=None):
        with self.hw_lock:
            self._check_open()
            if pan is None:
                pan = self.pan
            if tilt is None:
                tilt = self.tilt
            self.pan, self.tilt = self.robot.set_camera(pan, tilt, wait=False)
            ticket = self._bump(("pan", "tilt"))
            result = (self.pan, self.tilt)
        self.schedule(config.SERVO_SETTLE_S, self._relax_if_latest, ticket)
        return result

    def set_front_servo(self, angle):
        """Move the ultrasonic servo (internal: re-centre, scan, auto). No safety checks here."""
        with self.hw_lock:
            self._check_open()
            self.front = self.robot.set_front_servo(angle, wait=False)
            # Readings during the swing don't show the path ahead. Restart the "clear" count,
            # so clearing the flag needs OBSTACLE_CLEAR_COUNT readings (0.5 s) taken after
            # the move started: longer than the 0.3 s swing.
            self.clear_count = 0
            ticket = self._bump(("front",))
            result = self.front
        self.schedule(config.SERVO_SETTLE_S, self._relax_if_latest, ticket)
        return result

    def point_front_servo(self, angle):
        """The user points the ultrasonic (/api/front_servo)."""
        with self.hw_lock:
            self._check_open()
            if self.auto is not None:
                raise ApiError(409, "auto mode is using the ultrasonic: switch to manual first")
            if self.scanning:
                raise ApiError(409, "scan in progress: wait for it to finish")
            if self.cmd in shared.FORWARD_MOVES:
                # Pointed sideways, the sensor can't protect the path ahead (see sensor_tick).
                raise ApiError(409, "can't point the ultrasonic away while driving forward")
        return self.set_front_servo(angle)

    def front_is_ahead(self):
        """True if the ultrasonic points (close enough) straight ahead, so it measures our path."""
        return abs(self.front - config.FRONT_CENTER) <= config.FRONT_CENTER_TOLERANCE_DEG

    def read_distance_now(self):
        """One ultrasonic reading for a scan. Does NOT touch the obstacle flag."""
        with self.hw_lock:
            self._check_open()
            return self.robot.read_distance_cm()

    def read_line_now(self):
        with self.hw_lock:
            self._check_open()
            self.line = self.robot.read_line_sensors()
            return self.line

    def home_servos(self):
        """At startup the real servo positions are unknown: move them to match our state."""
        self.set_camera(config.PAN_CENTER, config.TILT_LEVEL)
        self.set_front_servo(config.FRONT_CENTER)

    # ---------- buzzer / LED ----------

    def beep(self, seconds):
        """Start a beep in the background and return at once."""
        seconds = shared.clamp_beep(seconds)
        thread = threading.Thread(target=self._beep_worker, args=(seconds,), name="beep")
        thread.daemon = True
        thread.start()
        self._beep_thread = thread
        return seconds

    def _beep_worker(self, seconds):
        with self.beep_lock:
            with self.hw_lock:
                if self.closed:
                    return
                self.robot.buzzer(True)
            try:
                self.sleep(seconds)  # buzzer sounds; hw_lock is free meanwhile
            finally:
                with self.hw_lock:
                    if not self.closed:  # cleanup() already silenced it
                        self.robot.buzzer(False)

    def set_led(self, r, g, b):
        """Manual colour (/api/led). Shown until the mode changes; an obstacle flash still wins."""
        with self.hw_lock:
            self._check_open()
            self.led = tuple(self.robot.set_led(r, g, b))
            self.led_override = self.led
            return self.led

    def _led_colour(self, now):
        """Which colour the LED should show now. Highest priority first. hw_lock held."""
        if self.obstacle:
            on = int(now / config.LED_FLASH_S) % 2 == 0
            return config.LED_OBSTACLE if on else config.LED_OFF
        if self.line_lost:
            return config.LED_LINE_LOST
        if self.led_override is not None:
            return self.led_override
        if self.auto is not None:
            return config.LED_AUTO
        return config.LED_MANUAL

    def led_tick(self, now=None):
        """Show the state colour. Only talks to the GPIO when the colour changes."""
        if now is None:
            now = self.clock()
        with self.hw_lock:
            if self.closed:
                return
            colour = self._led_colour(now)
            if colour != self.led:
                self.led = tuple(self.robot.set_led(*colour))

    # ---------- modes ----------

    def _set_mode(self, mode, reason):
        """hw_lock held. A mode change hands the LED back to the state colours."""
        self.mode = mode
        self.mode_reason = reason
        self.led_override = None
        self.line_lost = False
        if mode == "manual":
            self.auto = None

    def heartbeat(self):
        """Called by /api/status and /api/mode: the laptop is still there."""
        self.last_heartbeat = self.clock()

    def set_mode(self, mode):
        """Switch mode (/api/mode). Starting a mode always stops the motors first."""
        self.heartbeat()
        with self.hw_lock:
            self._check_open()
            if self.scanning:
                raise ApiError(409, "scan in progress: wait for it to finish")
            if mode == "line_follow" and self.obstacle:
                raise ApiError(409, "obstacle ahead: clear it before starting line follow")
            self._stop_motors()
            self._set_mode("manual", "mode change")  # old auto (if any) is cancelled here

        ready_at = self.clock()
        if mode != "manual" and not self.front_is_ahead():
            self.set_front_servo(config.FRONT_CENTER)  # auto needs the ultrasonic looking ahead
            ready_at += config.SERVO_SETTLE_S         # ...and waits until it gets there

        with self.hw_lock:
            self._check_open()
            if mode == "line_follow":
                self.auto = auto.LineFollower(ready_at)
            elif mode == "avoid":
                self.auto = auto.Avoider(ready_at)
            self._set_mode(mode, "")
            self.blind_stopped = False
        print("MODE -> %s" % mode)
        return mode

    def end_auto(self, owner, reason, line_lost=False):
        """An auto mode gives up (line lost, obstacle, ...): stop, beep, back to manual."""
        with self.hw_lock:
            if self.closed or self.auto is not owner:
                return  # already cancelled by something else
            self._stop_motors()
            self._set_mode("manual", reason)
            self.line_lost = line_lost
        print("AUTO %s ended: %s" % (owner.mode, reason))
        self.beep(config.OBSTACLE_BEEP_S)

    def auto_tick(self, now=None):
        """20x/s: run one step of the auto mode, after the heartbeat check."""
        if now is None:
            now = self.clock()
        with self.hw_lock:
            if self.closed or self.auto is None:
                return
            owner = self.auto
            silent_s = now - self.last_heartbeat
        if silent_s > config.AUTO_HEARTBEAT_S:
            # Laptop closed / Wi-Fi gone: nobody is watching the robot any more.
            self.end_auto(owner, "heartbeat lost")
            return
        owner.tick(self, now)

    # ---------- scan ----------

    def scan(self, angles=config.SCAN_ANGLES):
        """Point the ultrasonic at each angle and measure (/api/scan). Tank must be stopped.

        Runs in the request's thread: the Scan state machine + sleeps, about 2.5 s in total.
        The hardware lock is only held for each servo move / reading, never during the sleeps.
        """
        with self.hw_lock:
            self._check_open()
            if self.auto is not None:
                raise ApiError(409, "auto mode is running: switch to manual first")
            if self.cmd != "stop":
                raise ApiError(409, "stop the tank before scanning")
            if self.scanning:
                raise ApiError(409, "a scan is already running")
            self.scanning = True
        try:
            job = auto.Scan(angles)
            while not job.tick(self, self.clock()):
                self.sleep(config.AUTO_PERIOD_S)
        finally:
            with self.hw_lock:
                self.scanning = False
        print("SCAN %s" % (job.result(),))
        return job.result()

    # ---------- safety ----------

    def watchdog_tick(self, now=None):
        """Stop the motors if we are moving and no drive command came for WATCHDOG_TIMEOUT_S."""
        if now is None:
            now = self.clock()
        with self.hw_lock:
            # Auto modes send no drive commands over Wi-Fi: they have their own heartbeat check.
            if self.closed or self.cmd == "stop" or self.mode != "manual":
                return
            if now - self.last_drive_time > config.WATCHDOG_TIMEOUT_S:
                print("WATCHDOG: no drive command for %.2f s, stopping" % (now - self.last_drive_time))
                self._stop_motors()

    def _note_reading(self, distance):
        """Update the reading counters. hw_lock held."""
        self.distance_cm = distance
        if distance is None:
            self.none_count += 1
            self.clear_count = 0  # no echo is not proof the path is free
        else:
            self.none_count = 0
            self.last_real_cm = distance
            if distance > config.OBSTACLE_CLEAR_CM:
                self.clear_count += 1
            else:
                self.clear_count = 0

    def _set_obstacle(self):
        # hw_lock held.
        self.obstacle = True
        self.clear_count = 0

    def sensor_tick(self):
        """Read the sensors once and apply the obstacle rules.

        1. Stop fast: ONE real reading < OBSTACLE_STOP_CM while forward -> stop + flag + beep.
        2. Clear slowly: the flag clears only after OBSTACLE_CLEAR_COUNT real readings in a row
           > OBSTACLE_CLEAR_CM. None (no echo) never counts: the sensor also gives no echo
           when an object is very close or at an angle.
        3. Blind stop: BLIND_STOP_TICKS "no echo" readings in a row while forward, and the last
           real reading was < BLIND_STOP_NEAR_CM -> stop (no flag). After a far reading, no echo
           means open space, so we keep driving.
        4. Side readings count for nothing: while the ultrasonic points sideways (outside
           FRONT_CENTER_TOLERANCE_DEG) it doesn't measure the path ahead, so it can neither set
           nor clear the flag. The reading is still stored for the GUI.
        "Forward" = cmd forward; in auto modes also left/right (they drive one track forward).
        """
        with self.hw_lock:
            if self.closed:
                return
            distance = self.robot.read_distance_cm()
            self.line = self.robot.read_line_sensors()
            self.reading_seq += 1
            hit = blind = False
            if not self.front_is_ahead():
                self.distance_cm = distance
                self.clear_count = 0  # a clear run must be made of readings ahead
                self.none_count = 0
            else:
                self._note_reading(distance)
                if distance is not None:
                    self.blind_stopped = False
                forward = self.cmd == "forward" or (
                    self.mode != "manual" and self.cmd in shared.FORWARD_MOVES)
                hit = forward and distance is not None and distance < config.OBSTACLE_STOP_CM
                blind = (forward and self.none_count >= config.BLIND_STOP_TICKS
                         and self.last_real_cm is not None
                         and self.last_real_cm < config.BLIND_STOP_NEAR_CM)
            if hit:
                self._stop_motors()
                self._set_obstacle()
            elif blind:
                self._stop_motors()
                self.blind_stopped = True
            elif self.obstacle and self.clear_count >= config.OBSTACLE_CLEAR_COUNT:
                self.obstacle = False
                print("obstacle cleared (%d readings > %d cm)"
                      % (self.clear_count, config.OBSTACLE_CLEAR_CM))
        if hit:
            print("OBSTACLE at %.1f cm: stopped" % distance)
            self.beep(config.OBSTACLE_BEEP_S)
        elif blind:
            print("SENSOR BLIND: %d readings with no echo, last real reading %.1f cm: stopped"
                  % (self.none_count, self.last_real_cm))

    def _loop(self, tick, period):
        while not self._stop_event.is_set():
            try:
                tick()
            except Exception:
                traceback.print_exc()  # a safety thread must never die
            self._stop_event.wait(period)

    def _watchdog_and_led(self):
        self.watchdog_tick()
        self.led_tick()

    def start_threads(self):
        for name, tick, period in (("watchdog", self._watchdog_and_led, config.WATCHDOG_PERIOD_S),
                                   ("sensors", self.sensor_tick, config.SENSOR_PERIOD_S),
                                   ("auto", self.auto_tick, config.AUTO_PERIOD_S)):
            thread = threading.Thread(target=self._loop, args=(tick, period), name=name)
            thread.daemon = True
            thread.start()
            self._threads.append(thread)
        self.climate.start()
        self.soil.start()

    def shutdown(self):
        """Stop the threads, stop the motors, release the pins. Safe to call twice."""
        self._stop_event.set()
        for thread in self._threads:
            thread.join(1.0)
        try:
            with self.hw_lock:
                if self.closed:
                    return
                self.closed = True
                self.auto = None
                try:
                    self._stop_motors()
                finally:
                    self.robot.cleanup()
        finally:
            try:
                self.climate.close()  # DHT11 power pin LOW, even if the GPIO cleanup failed
            finally:
                self.soil.close()     # serial port closed, even if the steps above failed
        print("robot stopped and cleaned up")

    # ---------- status ----------

    def status(self):
        now = self.clock()
        climate_now = self.climate.snapshot(now)  # own locks, outside hw_lock
        soil_now = self.soil.snapshot(now)
        with self.hw_lock:
            return {
                "climate": climate_now,
                "soil": soil_now,
                "distance_cm": self.distance_cm,
                "line": self.line,
                "cmd": self.cmd,
                "speed": self.speed,
                "pan": self.pan,
                "tilt": self.tilt,
                "front": self.front,
                "mode": self.mode,
                "mode_reason": self.mode_reason,
                "obstacle": self.obstacle,
                "front_ahead": self.front_is_ahead(),
                "scanning": self.scanning,
                "led": list(self.led) if self.led is not None else None,
                "uptime_s": round(self.clock() - self.start_time, 1),
            }


# ---------- input validation ----------

def get_json_object():
    # force=True: accept the body even without a Content-Type header (easier with curl).
    # silent=True: bad JSON gives None instead of Flask's HTML error page.
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object, like {\"cmd\": \"forward\"}")
    return body


def get_number(body, key, default=None):
    """body[key] as a number. Missing -> default (or 400 if there is no default)."""
    if key not in body:
        if default is None:
            raise ApiError(400, "missing %r" % key)
        return default
    value = body[key]
    # bool is a subclass of int in Python, so True would count as 1 without this check.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(400, "%r must be a number" % key)
    if math.isnan(value) or math.isinf(value):
        raise ApiError(400, "%r must be a finite number" % key)
    return value


# ---------- Flask app ----------

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def create_app(controller):
    # The phone dashboard (robot/web) is served as static files under /static, the page at /.
    app = Flask(__name__, static_folder=WEB_DIR, static_url_path="/static")
    # Flask 1.0 tells browsers to cache static files for 12 h. Then a phone would keep an old
    # app.js after a redeploy. 0 = always ask again (the files are tiny).
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    @app.route("/", methods=["GET"])
    def index():
        return app.send_static_file("index.html")

    @app.errorhandler(ApiError)
    def api_error(err):
        return jsonify(ok=False, error=err.message), err.status

    @app.errorhandler(404)
    def not_found(err):
        return jsonify(ok=False, error="no such endpoint: %s" % request.path), 404

    @app.errorhandler(405)
    def wrong_method(err):
        return jsonify(ok=False, error="method %s not allowed here" % request.method), 405

    @app.errorhandler(500)
    def server_error(err):
        return jsonify(ok=False, error="internal error, see the server log"), 500

    @app.route("/api/drive", methods=["POST"])
    def api_drive():
        body = get_json_object()
        cmd = body.get("cmd")
        if not isinstance(cmd, str):
            raise ApiError(400, "'cmd' must be a string, one of %s"
                           % ", ".join(sorted(shared.DRIVE_TABLE)))
        try:
            shared.check_command(cmd)
        except ValueError as err:
            raise ApiError(400, str(err))
        speed = get_number(body, "speed", config.DEFAULT_SPEED)
        speed, recentred = controller.drive(cmd, speed)
        if recentred:  # tell the GUI why the ultrasonic slider jumped back
            return jsonify(ok=True, cmd=cmd, speed=speed, recentred=True)
        return jsonify(ok=True, cmd=cmd, speed=speed)

    @app.route("/api/mode", methods=["POST"])
    def api_mode():
        body = get_json_object()
        mode = body.get("mode")
        if not isinstance(mode, str) or mode not in config.MODES:
            raise ApiError(400, "'mode' must be one of %s" % ", ".join(config.MODES))
        return jsonify(ok=True, mode=controller.set_mode(mode))

    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        result = controller.scan()
        return jsonify(ok=True, angles=result["angles"], distances_cm=result["distances_cm"])

    @app.route("/api/camera", methods=["POST"])
    def api_camera():
        body = get_json_object()
        if "pan" not in body and "tilt" not in body:
            raise ApiError(400, "give 'pan' and/or 'tilt'")
        pan = get_number(body, "pan", controller.pan)
        tilt = get_number(body, "tilt", controller.tilt)
        pan, tilt = controller.set_camera(pan, tilt)
        return jsonify(ok=True, pan=pan, tilt=tilt)

    @app.route("/api/front_servo", methods=["POST"])
    def api_front_servo():
        body = get_json_object()
        angle = controller.point_front_servo(get_number(body, "angle"))
        return jsonify(ok=True, angle=angle)

    @app.route("/api/beep", methods=["POST"])
    def api_beep():
        body = get_json_object()
        seconds = controller.beep(get_number(body, "seconds", 0.2))
        return jsonify(ok=True, seconds=seconds)

    @app.route("/api/led", methods=["POST"])
    def api_led():
        body = get_json_object()
        r = get_number(body, "r")
        g = get_number(body, "g")
        b = get_number(body, "b")
        led = controller.set_led(r, g, b)
        return jsonify(ok=True, led=list(led))

    @app.route("/api/status", methods=["GET"])
    def api_status():
        controller.heartbeat()  # the GUI polls this every 0.3 s: proof the laptop is there
        return jsonify(controller.status())

    @app.route("/api/config", methods=["GET"])
    def api_config():
        # Use the address the client used to reach us (192.168.50.1 on the robot hotspot,
        # something else on SanguNet), so the URL always works for that client.
        host = request.host.split(":")[0]
        return jsonify(
            camera_stream_url="http://%s:%d/?action=stream" % (host, config.CAMERA_STREAM_PORT),
            servo={
                "PAN_RIGHT": config.PAN_RIGHT, "PAN_CENTER": config.PAN_CENTER,
                "PAN_LEFT": config.PAN_LEFT,
                "TILT_DOWN": config.TILT_DOWN, "TILT_LEVEL": config.TILT_LEVEL,
                "TILT_UP": config.TILT_UP,
                "FRONT_LEFT": config.FRONT_LEFT, "FRONT_CENTER": config.FRONT_CENTER,
                "FRONT_RIGHT": config.FRONT_RIGHT,
            },
            limits={
                "pan": [config.PAN_MIN, config.PAN_MAX],
                "tilt": [config.TILT_MIN, config.TILT_MAX],
                "front": [config.FRONT_MIN, config.FRONT_MAX],
                "speed": [config.SPEED_MIN, config.SPEED_MAX],
                "color": [config.COLOR_MIN, config.COLOR_MAX],
                "beep_s": [0, config.BEEP_MAX_S],
            },
            drive_commands=sorted(shared.DRIVE_TABLE),
            default_speed=config.DEFAULT_SPEED,
            watchdog_timeout_s=config.WATCHDOG_TIMEOUT_S,
            obstacle_stop_cm=config.OBSTACLE_STOP_CM,
            modes=list(config.MODES),
            scan_angles=list(config.SCAN_ANGLES),
        )

    return app


# ---------- startup ----------

def stop_bluetooth_app():
    """Yahboom's app drives the same motor pins; it restarts at the next boot (demo fallback)."""
    subprocess.call(["pkill", "-f", config.BLUETOOTH_APP])  # exit code 1 = wasn't running
    time.sleep(0.5)  # give it time to let go of the pins


def bluetooth_app_running(call=subprocess.call):
    """pgrep exit code 0 = found. (pgrep never matches its own process.)"""
    return call(["pgrep", "-f", config.BLUETOOTH_APP],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def wait_for_bluetooth_app(running=bluetooth_app_running, clock=time.monotonic, sleep=time.sleep,
                           timeout_s=config.BOOT_WAIT_S):
    """--boot only: wait until the desktop session has started Yahboom's app, so that OUR pkill
    comes after it (otherwise it would start later and fight us over the GPIO pins).
    Returns True if it appeared, False after timeout_s (then we just carry on)."""
    start = clock()
    while clock() - start < timeout_s:
        if running():
            print("BOOT: %s is running after %.0f s, stopping it" % (config.BLUETOOTH_APP,
                                                                  clock() - start))
            return True
        sleep(config.BOOT_POLL_S)
    print("BOOT: %s did not start within %d s, carrying on" % (config.BLUETOOTH_APP, timeout_s))
    return False


def guard_bluetooth_app(running=bluetooth_app_running, stop=stop_bluetooth_app,
                        clock=time.monotonic, sleep=time.sleep, duration_s=config.BOOT_GUARD_S):
    """--boot only, in a thread: for the first minutes, kill Yahboom's app if it still shows up
    late (a slow desktop session). Returns how many times it had to kill it."""
    kills = 0
    start = clock()
    while clock() - start < duration_s:
        if running():
            print("BOOT GUARD: %s appeared late, stopping it" % config.BLUETOOTH_APP)
            stop()
            kills += 1
        sleep(config.BOOT_GUARD_PERIOD_S)
    return kills


def port_in_use(port, host="127.0.0.1"):
    """True if something already listens on this port (e.g. the agriscout service while you start
    a second copy by hand). A connect test works the same on Linux and Windows."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _exit_on_sigterm(signum, frame):
    raise SystemExit(0)  # so `kill <pid>` / `systemctl stop` also runs the finally: below


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgriScout robot server")
    parser.add_argument("--fake", action="store_true", help="use FakeRobot (no hardware)")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT)
    parser.add_argument("--boot", action="store_true",
                        help="started by agriscout.service at boot: wait for Yahboom's app first")
    args = parser.parse_args(argv)

    # Port guard, BEFORE any GPIO: a second copy would set up the pins, fail to open the port,
    # and then GPIO.cleanup() would pull the pins from under the running server.
    if port_in_use(args.port):
        print("Port %d is already in use: the server is already running (probably the "
              "agriscout service). Stop it first: sudo systemctl stop agriscout" % args.port)
        return 1

    monitor = soil_monitor = None
    if args.fake:
        robot = FakeRobot()
        if config.CLIMATE_ENABLED:
            monitor = climate.ClimateMonitor(fake_connect_pigpio, FakeDht11)
        if config.SOIL_ENABLED:
            soil_monitor = soil.SoilMonitor(FakeSoil, fake_find_soil_ports)
    else:
        if args.boot:
            wait_for_bluetooth_app()
        stop_bluetooth_app()
        import hardware  # only here: Windows has no RPi.GPIO
        robot = hardware.RealRobot()
        if args.boot:
            guard = threading.Thread(target=guard_bluetooth_app, name="boot-guard")
            guard.daemon = True
            guard.start()
        if config.CLIMATE_ENABLED:
            monitor = climate.ClimateMonitor(
                hardware.connect_pigpio,
                lambda pi: hardware.Dht11(pi, config.DHT11_PIN, power_pin=config.DHT11_POWER_PIN))
        if config.SOIL_ENABLED:
            soil_monitor = soil.SoilMonitor(soil.open_serial, soil.find_ports)

    controller = RobotController(robot, monitor, soil_monitor)
    app = create_app(controller)
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        controller.home_servos()
        controller.start_threads()
        print("AgriScout server on http://0.0.0.0:%d (Ctrl+C to stop)" % args.port)
        # use_reloader=False: the reloader starts a 2nd process that would grab the GPIO too.
        app.run(host="0.0.0.0", port=args.port, threaded=True, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        controller.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())  # exit code 1 (port in use) = "failed" for systemd
