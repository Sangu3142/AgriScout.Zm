"""Automatic behaviours as small state machines. Runs on the Pi (Python 3.7).

Every class has tick(ctl, now): the caller passes the time, and tick never sleeps. So:
  - the auto thread calls tick() 20x/s and one slow step can't block the safety threads
  - tests call tick() with made-up times, no real waiting
All hardware access goes through the RobotController (ctl), which holds the hardware lock.
"""

import config
import shared


class Scan(object):
    """Point the ultrasonic at each angle, wait for the servo, take the median of 3 readings.

    At the end the sensor is re-centred (and we wait for it), so forward readings are valid again.
    Used by POST /api/scan and by avoid's LOOK step.
    """

    def __init__(self, angles):
        self.angles = list(angles)
        self.distances = []
        self.index = 0          # angle being measured; len(angles) = the final re-centre
        self.state = "move"     # move -> settle -> (measure) -> move ...
        self.wait_until = 0.0
        self.done = False

    def tick(self, ctl, now):
        """Do the next step. Returns True when the scan is finished."""
        if self.done:
            return True
        if self.state == "move":
            if self.index < len(self.angles):
                ctl.set_front_servo(self.angles[self.index])
            else:
                ctl.set_front_servo(config.FRONT_CENTER)
            self.wait_until = now + config.SERVO_SETTLE_S
            self.state = "settle"
        elif now >= self.wait_until:  # servo has arrived
            if self.index < len(self.angles):
                readings = [ctl.read_distance_now() for _ in range(config.SCAN_READINGS)]
                self.distances.append(shared.median_cm(readings))
                self.index += 1
                self.state = "move"
            else:
                self.done = True
        return self.done

    def result(self):
        return {"angles": self.angles, "distances_cm": self.distances}


class LineFollower(object):
    """Follow a black line with the 4 IR sensors (Yahboom's rules, shared.line_command)."""
    mode = "line_follow"

    def __init__(self, ready_at):
        self.ready_at = ready_at      # wait until the ultrasonic servo is centred
        self.last_cmd = "stop"
        self.hold_until = 0.0         # sharp corner: keep the spin until then
        self.last_black = ready_at    # last time any sensor saw the line

    def tick(self, ctl, now):
        if now < self.ready_at:
            return
        if ctl.obstacle or ctl.blind_stopped:
            ctl.end_auto(self, "obstacle")  # decision A: stop and hand back to the user
            return

        if now < self.hold_until:
            cmd = self.last_cmd  # still turning a sharp corner (tracking.py:148 sleeps 80 ms)
        else:
            cmd, hold_s = shared.line_command(ctl.read_line_now())
            if cmd is None:  # all white
                if now - self.last_black >= config.LINE_LOST_S:
                    ctl.end_auto(self, "line lost", line_lost=True)
                    return
                cmd = self.last_cmd  # keep going as before, the line may come back
            else:
                self.last_black = now
                if hold_s:
                    self.hold_until = now + hold_s

        if cmd == "stop":
            ctl.auto_drive(self, "stop", 0)
        else:
            ctl.auto_drive(self, cmd, shared.line_speed(cmd))
        self.last_cmd = cmd


class Avoider(object):
    """Drive forward; on an obstacle: stop, back up, look left and right, spin to the clearer side.

    DRIVE -> BACK (AVOID_BACK_S) -> LOOK (Scan left + right) -> SPIN -> DRIVE
    SPIN ends when the sensor (pointing ahead) reads > AVOID_CLEAR_CM twice in a row and the
    obstacle flag is clear. Both sides blocked -> spin at least AVOID_UTURN_S (~180 degrees).
    Spinning longer than AVOID_SPIN_MAX_S -> stop, manual, "no way out".
    """
    mode = "avoid"

    def __init__(self, ready_at):
        self.state = "start"
        self.until = ready_at
        self.scan = None
        self.spin_cmd = None
        self.spin_started = 0.0
        self.min_until = 0.0
        self.clear_seen = 0
        self.seen_seq = None
        self.choice = None  # last LOOK result, for logs and tests

    def blocked(self, ctl):
        if ctl.obstacle or ctl.blind_stopped:
            return True
        d = ctl.distance_cm
        return ctl.front_is_ahead() and d is not None and d < config.AVOID_STOP_CM

    def tick(self, ctl, now):
        if self.state == "start":
            if now >= self.until:
                self.state = "drive"
        elif self.state == "drive":
            if self.blocked(ctl):
                ctl.auto_drive(self, "back", config.AVOID_SPEED)
                self.state = "back"
                self.until = now + config.AVOID_BACK_S
            else:
                ctl.auto_drive(self, "forward", config.AVOID_SPEED)
        elif self.state == "back":
            if now >= self.until:
                ctl.auto_drive(self, "stop", 0)
                self.scan = Scan([config.FRONT_LEFT, config.FRONT_RIGHT])
                self.state = "look"
        elif self.state == "look":
            if self.scan.tick(ctl, now):
                self._start_spin(ctl, now)
        elif self.state == "spin":
            self._spin(ctl, now)

    def _start_spin(self, ctl, now):
        left_cm, right_cm = self.scan.distances
        self.choice = shared.pick_side(left_cm, right_cm)
        print("AVOID: left %s cm, right %s cm -> %s" % (left_cm, right_cm, self.choice))
        if self.choice == "uturn":
            self.spin_cmd = "spin_right"
            self.min_until = now + config.AVOID_UTURN_S
        else:
            self.spin_cmd = self.choice
            self.min_until = now + config.AVOID_SPIN_MIN_S
        self.spin_started = now
        self.clear_seen = 0
        self.seen_seq = ctl.reading_seq  # only count readings taken from now on
        self.state = "spin"
        ctl.auto_drive(self, self.spin_cmd, config.AVOID_SPIN_SPEED)

    def _spin(self, ctl, now):
        if now - self.spin_started > config.AVOID_SPIN_MAX_S:
            ctl.end_auto(self, "no way out")
            return
        ctl.auto_drive(self, self.spin_cmd, config.AVOID_SPIN_SPEED)
        if ctl.reading_seq != self.seen_seq:  # a NEW reading (sensor runs at 10 Hz, we at 20 Hz)
            self.seen_seq = ctl.reading_seq
            d = ctl.distance_cm
            if ctl.front_is_ahead() and d is not None and d > config.AVOID_CLEAR_CM:
                self.clear_seen += 1
            else:
                self.clear_seen = 0
        if (now >= self.min_until and self.clear_seen >= config.AVOID_CLEAR_READINGS
                and not ctl.obstacle):
            self.state = "drive"
