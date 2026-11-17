"""After a batch scan (desktop/gui.py): the status line and the robot feedback (LED + beeps).

FeedbackThread runs for real, in its own thread, against FakeRobot: a stand-in for ApiClient that
records every call with its time. The waits are made short.
"""

import os
import time
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402
from api_client import Result  # noqa: E402
from scout import config  # noqa: E402
from scout.classifier import StubClassifier  # noqa: E402
from scout.pipeline import scan_plant  # noqa: E402
from test_scout_pipeline import FixedClassifier, photo  # noqa: E402

GREEN, RED, YELLOW = (0, 255, 0), (255, 0, 0), (255, 255, 0)
WHITE, OFF = (255, 255, 255), (0, 0, 0)
BEEP = ("beep", 0.1)
RESTORE = [("status",), ("led", GREEN)]  # mode still manual? -> the manual colour back


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- status line

@pytest.fixture
def scan(scout_db, tmp_path):
    """scan(plant_id) -> a real scan_plant() result, exactly what on_plant_scan_done gets."""
    def run(plant_id, classifier=None):
        classifier = classifier or StubClassifier.from_file(config.STUB_SCRIPT)
        return scan_plant(classifier, scout_db, image_bytes=photo(plant_id),
                          scans_dir=str(tmp_path / "scans"))
    return run


def test_status_line_does_not_repeat_not_sure(scan):
    assert gui.scan_summary(scan("T-03")) == "last: T-03  Not sure - scan again"  # stub: 52 %


def test_status_line_no_tomato_or_pepper(scan):
    result = scan("T-01", FixedClassifier([("background", 0.9)]))
    assert gui.scan_summary(result) == "last: T-01  No tomato or green pepper found"


def test_status_line_names_a_confident_diagnosis(scan):
    assert gui.scan_summary(scan("T-02")) == "last: T-02  Remove  (Spoiled tomato)"
    assert gui.scan_summary(scan("P-01")) == "last: P-01  Good  (Fresh green pepper)"


# ---------------------------------------------------------------- robot feedback

class FakeRobot:
    """Stands in for ApiClient: records (time, name, args); status() reports self.mode."""

    def __init__(self, mode="manual", reachable=True, delay=0.0):
        self.mode, self.reachable, self.delay = mode, reachable, delay
        self.calls = []
        self.closed = False

    def _answer(self, name, *args):
        time.sleep(self.delay)  # a slow Wi-Fi reply
        self.calls.append((time.monotonic(), name, args))
        if not self.reachable:
            return Result(False, error="cannot reach robot")
        return Result(True, data={"mode": self.mode})

    def status(self):
        return self._answer("status")

    def led(self, r, g, b):
        return self._answer("led", (r, g, b))

    def beep(self, seconds=0.2):
        return self._answer("beep", seconds)

    def close(self):
        self.closed = True


def calls(robot):
    """[("status",), ("led", (0, 255, 0)), ("beep", 0.1), ...]: the calls without their times."""
    return [(name,) + args for _, name, args in robot.calls]


def times(robot, call):
    return [t for t, name, args in robot.calls if (name,) + args == call]


def wait_for(condition, timeout=3.0):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


@pytest.fixture
def start(app, monkeypatch):
    """start(robot) -> a running FeedbackThread talking to robot. Short waits."""
    monkeypatch.setattr(gui, "FEEDBACK_LED_S", 0.4)
    monkeypatch.setattr(gui, "FEEDBACK_BLINK_S", 0.03)
    monkeypatch.setattr(gui, "FEEDBACK_BEEP_GAP_S", 0.05)
    threads = []

    def run(robot):
        thread = gui.FeedbackThread(make_client=lambda address, timeout: robot)
        thread.start()
        threads.append(thread)
        return thread
    yield run
    for thread in threads:
        thread.shutdown()
        assert thread.wait(5000)


def restored(robot):
    return calls(robot)[-2:] == RESTORE


def test_good_blinks_green_3_times_with_one_beep(start):
    robot = FakeRobot()
    start(robot).show("robot", "healthy")
    wait_for(lambda: restored(robot))
    assert calls(robot) == [("status",), BEEP,
                            ("led", OFF), ("led", GREEN), ("led", OFF), ("led", GREEN),
                            ("led", OFF), ("led", GREEN)] + RESTORE
    # The blink timing is checked on a fake clock below: measured wall-clock gaps were flaky.
    first, restore = times(robot, ("status",))
    assert restore - first >= gui.FEEDBACK_LED_S


class FakeClock:
    """gui.time stand-in: time moves only when a fake request or a fake wait says so."""

    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


@pytest.mark.parametrize("request_s", [0.0, 0.1])   # instant reply / a slow Wi-Fi reply
def test_blink_phases_keep_the_blink_period(app, monkeypatch, request_s):
    clock = FakeClock()
    monkeypatch.setattr(gui, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    leds, waits = [], []

    def led(*colour):
        clock.now += request_s
        leds.append((clock.now, colour))

    def wait(seconds):
        waits.append(seconds)
        clock.now += max(seconds, 0.0)
        return True

    thread = gui.FeedbackThread(make_client=None)   # never started: _blink is called directly
    thread._wait = wait
    assert thread._blink(types.SimpleNamespace(led=led), GREEN, 3)
    assert [colour for _, colour in leds] == [OFF, GREEN] * 3
    # each off / on phase lasts exactly FEEDBACK_BLINK_S; a slow request shortens only the wait
    stamps = [t for t, _ in leds]
    assert [b - a for a, b in zip(stamps, stamps[1:])] == pytest.approx([gui.FEEDBACK_BLINK_S] * 5)
    assert waits == pytest.approx([gui.FEEDBACK_BLINK_S - request_s] * 6)
    assert all(w > 0 for w in waits)   # visible off / on phases


def test_remove_is_solid_red_with_3_separate_beeps(start):
    robot = FakeRobot()
    start(robot).show("robot", "action")
    wait_for(lambda: restored(robot))
    assert calls(robot) == [("status",), ("led", RED), BEEP, BEEP, BEEP] + RESTORE
    beeps = times(robot, BEEP)
    assert all(b - a >= gui.FEEDBACK_BEEP_GAP_S for a, b in zip(beeps, beeps[1:]))
    red_for = times(robot, ("status",))[1] - times(robot, ("led", RED))[0]
    assert red_for >= gui.FEEDBACK_LED_S - 0.01


@pytest.mark.parametrize("status, colour", [("uncertain", YELLOW), ("no_produce", WHITE)])
def test_not_sure_and_no_produce_are_silent_solid_colours(start, status, colour):
    robot = FakeRobot()
    start(robot).show("robot", status)
    wait_for(lambda: restored(robot))
    assert calls(robot) == [("status",), ("led", colour)] + RESTORE


@pytest.mark.parametrize("status, expected", [
    ("healthy", [("status",), BEEP]),
    ("action", [("status",), BEEP, BEEP, BEEP]),
    ("uncertain", [("status",)]),
    ("no_produce", [("status",)]),
])
def test_auto_mode_only_beeps(start, status, expected):
    robot = FakeRobot(mode="line_follow")      # the LED shows the mode: never touched
    start(robot).show("robot", status)
    time.sleep(gui.FEEDBACK_LED_S + 0.3)       # long enough for a restore, if there were one
    assert calls(robot) == expected


def test_auto_started_during_the_signal_keeps_its_colour(start):
    robot = FakeRobot()
    start(robot).show("robot", "uncertain")
    wait_for(lambda: ("led", YELLOW) in calls(robot))
    robot.mode = "avoid"                       # the robot already dropped our colour for blue
    wait_for(lambda: len(robot.calls) == 3)    # the restore's status check
    time.sleep(0.1)
    assert calls(robot) == [("status",), ("led", YELLOW), ("status",)]   # no green over blue


def test_check_and_other_statuses_send_nothing(start):
    robot = FakeRobot()
    thread = start(robot)
    thread.show("robot", "watch")              # no knowledge base class gives it
    thread.show("robot", "none")
    time.sleep(0.2)
    assert robot.calls == []


def test_robot_not_reachable_is_skipped_silently(start):
    robot = FakeRobot(reachable=False)
    thread = start(robot)
    thread.show("robot", "action")
    time.sleep(gui.FEEDBACK_LED_S + 0.3)
    assert calls(robot) == [("status",)]       # no LED, no beeps, no exception
    assert thread.isRunning()                  # still there for the next scan


def test_newer_result_replaces_the_running_one(start, monkeypatch):
    monkeypatch.setattr(gui, "FEEDBACK_BEEP_GAP_S", 0.3)   # time to arrive between two beeps
    robot = FakeRobot()
    thread = start(robot)
    thread.show("robot", "action")
    wait_for(lambda: BEEP in calls(robot))
    thread.show("robot", "uncertain")
    wait_for(lambda: restored(robot))
    time.sleep(0.1)
    assert calls(robot) == ([("status",), ("led", RED), BEEP]           # cut after 1 beep
                            + [("status",), ("led", YELLOW)]            # no restore in between
                            + RESTORE)


def test_closing_puts_the_mode_colour_back_and_ends(start, monkeypatch):
    monkeypatch.setattr(gui, "FEEDBACK_LED_S", 30.0)       # a long signal, cut by closing
    robot = FakeRobot()
    thread = start(robot)
    thread.show("robot", "no_produce")
    wait_for(lambda: ("led", WHITE) in calls(robot))
    started = time.monotonic()
    thread.shutdown()
    assert thread.wait(2000) and time.monotonic() - started < 1.0
    assert calls(robot) == [("status",), ("led", WHITE)] + RESTORE
    assert robot.closed


def test_show_never_waits_for_the_robot(start):
    robot = FakeRobot(delay=0.3)               # every request takes 0.3 s
    thread = start(robot)
    started = time.monotonic()
    thread.show("robot", "action")
    thread.show("robot", "healthy")
    assert time.monotonic() - started < 0.05


def test_new_address_gets_a_new_client(app):
    made = []

    def make_client(address, timeout):
        made.append((address, timeout))
        return FakeRobot(mode="line_follow")
    thread = gui.FeedbackThread(make_client=make_client)
    thread.start()
    try:
        for address in ("192.168.50.1:5000", "192.168.50.1:5000", "127.0.0.1:5000"):
            thread.show(address, "uncertain")
        wait_for(lambda: len(made) == 2)
    finally:
        thread.shutdown()
        assert thread.wait(2000)
    assert made == [("192.168.50.1:5000", gui.FEEDBACK_TIMEOUT_S),
                    ("127.0.0.1:5000", gui.FEEDBACK_TIMEOUT_S)]


# ---------------------------------------------------------------- "Robot lights & beeps" setting

STATUSES = ["healthy", "action", "uncertain", "no_produce", "watch"]
SENT = {
    "off": [],                                                # default: the tank stays quiet
    "problems": ["action", "uncertain", "no_produce"],        # Remove, Not sure, no produce
    "every": ["healthy", "action", "uncertain", "no_produce"],
}


@pytest.mark.parametrize("choice", list(SENT))
@pytest.mark.parametrize("status", STATUSES)
def test_feedback_wanted(choice, status):
    assert gui.feedback_wanted(choice, status) == (status in SENT[choice])


def window_stand_in(choice, feedback):
    """Just what MainWindow.send_scan_feedback uses."""
    return types.SimpleNamespace(feedback_choice=choice, address="robot", feedback=feedback)


@pytest.mark.parametrize("choice", list(SENT))
def test_send_scan_feedback_follows_the_choice(choice):
    shown = []
    window = window_stand_in(choice, types.SimpleNamespace(
        show=lambda address, status: shown.append((address, status))))
    for status in STATUSES:
        gui.MainWindow.send_scan_feedback(window, status)
    assert shown == [("robot", status) for status in SENT[choice]]


def test_off_sends_nothing_to_the_robot(start):
    robot = FakeRobot()
    window = window_stand_in("off", start(robot))     # the real FeedbackThread
    for status in STATUSES:
        gui.MainWindow.send_scan_feedback(window, status)
    time.sleep(gui.FEEDBACK_LED_S + 0.3)              # long enough for a signal + restore
    assert robot.calls == []                          # not even a status request: LED untouched


def test_feedback_choice_default_off_saved_and_reloaded(app, tmp_path):
    path = str(tmp_path / "gui.ini")
    assert gui.FEEDBACK_DEFAULT == "off"
    assert gui.load_feedback_choice(QSettings(path, QSettings.IniFormat)) == "off"  # first start
    for choice in ("problems", "every", "off"):
        gui.save_feedback_choice(QSettings(path, QSettings.IniFormat), choice)
        assert gui.load_feedback_choice(QSettings(path, QSettings.IniFormat)) == choice  # restart
    gui.save_feedback_choice(QSettings(path, QSettings.IniFormat), "every")
    settings = QSettings(path, QSettings.IniFormat)
    settings.setValue(gui.FEEDBACK_SETTING_KEY, "loud")      # not one of the 3 choices
    settings.sync()
    assert QSettings(path, QSettings.IniFormat).value(gui.FEEDBACK_SETTING_KEY) == "loud"
    assert gui.load_feedback_choice(QSettings(path, QSettings.IniFormat)) == "off"
