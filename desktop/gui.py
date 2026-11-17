"""AgriScout desktop GUI (Windows, PySide6). Talks to the robot's Flask API through ApiClient.

Rule: the GUI thread never waits on the network. Worker threads do that:
  VideoThread    reads the MJPEG stream with OpenCV, keeps only the newest frame
  StatusThread   asks /api/status every 0.3 s
  CommandThread  sends drive/camera/beep requests, in order, one at a time
  ScanThread     (one per batch scan) snapshot -> QR -> diagnosis -> database (scout/ package)
  FeedbackThread shows each batch scan result on the tank: LED signal + beeps (only if the
                 user turned it on: "Robot lights & beeps after a scan", default Off)

Usage:
  python desktop\\gui.py                      connects to 192.168.50.1:5000 (robot hotspot)
  python desktop\\gui.py --address 127.0.0.1:5000   e.g. against robot_server.py --fake
  AgriScout.lnk (desktop\\make_shortcut.ps1)      same, with pythonw: no console window
"""

if __name__ == "__main__":
    # First thing, before cv2/PySide6: with pythonw there is no console, so even a failed
    # import must end up in the log. (Only when run as the app, not when tests import gui.)
    import crash_log
    crash_log.install()

import argparse
import ctypes
import dataclasses
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import cv2
from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QStyle, QVBoxLayout, QWidget)


try:  # the result card is HTML (scout/templates/card.html), shown by Qt's Chromium view
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    WEBENGINE_ERROR = ""
except ImportError as err:  # PySide6 without the Addons package: scanning is disabled, not the GUI
    QWebEnginePage = QWebEngineView = None
    WEBENGINE_ERROR = "Qt WebEngine missing (%s)" % err

from api_client import (DEFAULT_ADDRESS, ApiClient, Climate, Result, Soil, parse_climate,
                        parse_soil)

# scout/ (plant scanning) is a package in the repo root, one level above this file.
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_DIR not in sys.path:
    sys.path.append(REPO_DIR)
from scout import config as scout_config  # noqa: E402  (needs the sys.path line above)
from scout import db as scout_db  # noqa: E402
from scout.card import (ACTION_BASE, STATUS_TEXT, STUB_BANNER, card_data,  # noqa: E402
                        export_name, render_card)
from scout.classifier import is_stub  # noqa: E402
from scout.pipeline import ScanError, make_classifier, scan_plant  # noqa: E402
from scout.report import write_report  # noqa: E402

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agriscout.ico")
DRIVE_REPEAT_MS = 150      # while a drive button/key is held, resend the command this often
STATUS_PERIOD_S = 0.3
CAMERA_DEBOUNCE_MS = 100   # slider moves are sent at most this often
FAILS_BEFORE_RED = 3       # one slow reply on the Wi-Fi shouldn't flash the light red
VIDEO_TIMEOUT_MS = 3000    # OpenCV gives up opening/reading the stream after this
VIDEO_RETRY_S = 2.0

SCAN_BAR_MAX_CM = 200     # scan bars are full at this distance
BIG_BUTTON_H = 40         # drive pad / mode buttons: easy to hit, but fits a 768 px screen
MODE_BUTTONS = [("Manual", "manual"), ("Line follow", "line_follow"), ("Avoid", "avoid")]
LED_BUTTONS = [("Off", (0, 0, 0)), ("Red", (255, 0, 0)), ("Green", (0, 255, 0)),
               ("Blue", (0, 0, 255)), ("White", (255, 255, 255))]

# Robot feedback after a batch scan (FeedbackThread). The signals differ from every normal LED
# colour (robot/config.py): manual = green, auto = blue, obstacle = red flashing.
LED_OFF = (0, 0, 0)
LED_MANUAL = (0, 255, 0)  # = robot/config.py LED_MANUAL; the API doesn't report it
# status -> (colour, blinks, beeps); 0 blinks = solid colour. "watch" (Check) has no entry: no
# knowledge base class has low severity, so no scan gets it.
SCAN_FEEDBACK = {
    "healthy": ((0, 255, 0), 3, 1),         # Good: green blinking 3 times + 1 beep
    "action": ((255, 0, 0), 0, 3),          # Remove: solid red + 3 beeps
    "uncertain": ((255, 255, 0), 0, 0),     # Not sure: solid yellow
    "no_produce": ((255, 255, 255), 0, 0),  # no tomato/pepper: solid white (blue = auto)
}
FEEDBACK_BLINK_S = 0.3      # each off / on phase of the Good blink
FEEDBACK_BEEP_S = 0.1       # one short beep (as long as the robot's obstacle beep)
FEEDBACK_BEEP_GAP_S = 0.25  # wait after each beep reply: the robot plays queued beeps back to back
FEEDBACK_LED_S = 5.0        # then the colour of the current mode comes back
FEEDBACK_TIMEOUT_S = 1.5    # Wi-Fi replies can take 0.7 s; nothing waits on this thread anyway
# GUI setting "Robot lights & beeps after a scan": (shown text, id). Remembered in QSettings.
FEEDBACK_CHOICES = [("Off", "off"), ("Problems only", "problems"), ("Every scan", "every")]
FEEDBACK_DEFAULT = "off"    # the tank stays quiet unless the user turns it on
FEEDBACK_SETTING_KEY = "scan_feedback"
PROBLEM_STATUSES = {"action", "uncertain", "no_produce"}  # Remove, Not sure, no tomato/pepper

CARD_WIDTH = 480           # Save / Share PNG: the phone-width card (good for WhatsApp)
CARD_HEIGHT = 900
WINDOW_SCREEN_SHARE = 0.9  # scan result window: this share of the screen, both ways

KEY_TO_CMD = {
    Qt.Key_W: "forward", Qt.Key_S: "back", Qt.Key_A: "left", Qt.Key_D: "right",
    Qt.Key_Q: "spin_left", Qt.Key_E: "spin_right",
}


def feedback_wanted(choice: str, status: str) -> bool:
    """Should this scan result be shown on the tank, with this "lights & beeps" choice?"""
    if choice == "every":
        return status in SCAN_FEEDBACK
    if choice == "problems":
        return status in PROBLEM_STATUSES
    return False  # "off" (or an unknown value): nothing is sent to the robot


CLIMATE_SENSOR_MAX_PCT = 90  # the DHT11 tops out at ~90-91 % RH: higher readings mean "at least 90"
CLIMATE_GREY = "color: #888;"  # stale / offline / waiting: not a reading to trust right now


def age_text(age_s: float) -> str:
    """4.7 -> '4 s', 150 -> '2 min'."""
    seconds = int(age_s)
    return f"{seconds} s" if seconds < 120 else f"{seconds // 60} min"


def climate_text(c: Climate) -> tuple[str, str]:
    """(text, state) for the Climate panel. state: fresh, stale, waiting or offline.

    '28.6 °C · 57 % RH · updated 4 s ago'; ' (stale)' added when older than CLIMATE_FRESH_S.
    """
    if not c.available:
        return f"Sensor offline: {c.reason or 'unknown reason'}", "offline"
    if c.temp_c is None or c.humidity_pct is None or c.age_s is None:
        return "Waiting for the first reading...", "waiting"
    if c.humidity_pct >= CLIMATE_SENSOR_MAX_PCT:
        humidity = f"≥ {CLIMATE_SENSOR_MAX_PCT} % RH (sensor maximum)"
    else:
        humidity = f"{c.humidity_pct:.0f} % RH"
    text = f"{c.temp_c:.1f} °C · {humidity} · updated {age_text(c.age_s)} ago"
    if c.age_s > scout_config.CLIMATE_FRESH_S:
        return text + " (stale)", "stale"
    return text, "fresh"


SOIL_STATE_TEXT = {"dry": "Dry", "ok": "OK", "wet": "Wet"}


def soil_text(s: Soil) -> tuple[str, str]:
    """(text, state) for the Soil panel. state: fresh, stale or offline. Information only.

    '57 % moisture · OK · raw 600 · updated 1 s ago'; stale (the robot keeps the last line but no
    new one came): '57 % moisture · OK · raw 600 (no reading for 12 s)'; else the reason.
    """
    if s.raw is None or s.moisture_pct is None:
        return f"Soil: {s.reason or 'unknown reason'}", "offline"
    text = (f"{s.moisture_pct} % moisture · {SOIL_STATE_TEXT.get(s.state, '?')} · "
            f"raw {s.raw}")
    if not s.available:
        return f"{text} ({s.reason or 'stale'})", "stale"
    if s.age_s is not None:
        text += f" · updated {age_text(s.age_s)} ago"
    return text, "fresh"


def climate_for_scan(last: tuple[Climate, float] | None, now: float) -> dict | None:
    """The newest polled climate as /api/status sent it, with its age moved on to `now`
    (time.monotonic()). If the robot stopped answering, the age keeps growing, so the scan
    stores nothing once it passes 30 s (scout.storage.climate_columns)."""
    if last is None:
        return None
    climate, received_at = last
    data = dataclasses.asdict(climate)
    if climate.age_s is not None:
        data["age_s"] = climate.age_s + max(0.0, now - received_at)
    return data


def open_settings() -> QSettings:
    """The GUI's remembered settings (Windows: registry HKCU\\Software\\AgriScout)."""
    return QSettings("AgriScout", "AgriScout GUI")


def load_feedback_choice(settings: QSettings) -> str:
    """The saved "lights & beeps" choice, or FEEDBACK_DEFAULT if none (or not a known one)."""
    value = settings.value(FEEDBACK_SETTING_KEY, FEEDBACK_DEFAULT)
    return value if value in [cid for _, cid in FEEDBACK_CHOICES] else FEEDBACK_DEFAULT


def save_feedback_choice(settings: QSettings, choice: str) -> None:
    settings.setValue(FEEDBACK_SETTING_KEY, choice)
    settings.sync()  # written now, not only when the GUI closes cleanly


def sleep_unless_interrupted(thread: QThread, seconds: float) -> None:
    """Sleep in small steps so closing the window never waits for a long sleep."""
    end = time.monotonic() + seconds
    while not thread.isInterruptionRequested() and time.monotonic() < end:
        thread.msleep(50)


# ---------------------------------------------------------------- worker threads

class VideoThread(QThread):
    """Reads the MJPEG stream and hands the GUI only the newest frame.

    Low latency: the loop reads frames as fast as the stream sends them (so no backlog builds up
    inside OpenCV), and overwrites self._latest each time. The GUI is only told "a frame is ready"
    when it has taken the previous one. If the GUI is slow, old frames are dropped, never queued.
    """
    frame_ready = Signal(object)    # arg: this thread (so the GUI can ignore an old thread)
    no_video = Signal(object, str)  # arg: this thread, reason

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self._lock = threading.Lock()
        self._latest: QImage | None = None
        self._pending = False  # True = GUI was told about a frame and hasn't taken it yet

    def take_frame(self) -> QImage | None:
        with self._lock:
            image, self._latest, self._pending = self._latest, None, False
        return image

    def run(self) -> None:
        while not self.isInterruptionRequested():
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG,
                                   [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, VIDEO_TIMEOUT_MS,
                                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, VIDEO_TIMEOUT_MS])
            if not cap.isOpened():
                cap.release()
                self.no_video.emit(self, "no video stream at " + self.url)
                sleep_unless_interrupted(self, VIDEO_RETRY_S)
                continue
            # Ask OpenCV to keep at most 1 buffered frame. Some backends ignore it (returns False),
            # which is fine: the read-everything loop below drains the buffer anyway.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            while not self.isInterruptionRequested():
                ok, frame = cap.read()
                if not ok:
                    break
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                height, width, _ = rgb.shape
                # .copy(): QImage would otherwise point into numpy memory that gets reused.
                image = QImage(rgb.data, width, height, 3 * width, QImage.Format_RGB888).copy()
                with self._lock:
                    self._latest = image
                    tell_gui = not self._pending
                    self._pending = True
                if tell_gui:
                    self.frame_ready.emit(self)
            cap.release()
            if not self.isInterruptionRequested():
                self.no_video.emit(self, "video stream dropped, reconnecting...")
                sleep_unless_interrupted(self, VIDEO_RETRY_S)


class StatusThread(QThread):
    """Polls /api/status every STATUS_PERIOD_S. A loop (not a queue), so slow replies never pile up."""
    status = Signal(str, object)  # address asked, Result

    def __init__(self):
        super().__init__()
        self._address: str | None = None

    def set_address(self, address: str) -> None:
        self._address = address  # read by run(); a plain assignment is atomic in Python

    def run(self) -> None:
        client, current = None, None
        while not self.isInterruptionRequested():
            address = self._address
            if address != current:
                if client:
                    client.close()
                client, current = ApiClient(address), address
            started = time.monotonic()
            if client:
                self.status.emit(current, client.status())
            sleep_unless_interrupted(self, STATUS_PERIOD_S - (time.monotonic() - started))
        if client:
            client.close()


class CommandThread(QThread):
    """Runs GUI commands in order, one at a time, so a button click never blocks the window."""
    done = Signal(str, object, object)  # action name, args tuple, Result

    def __init__(self):
        super().__init__()
        self._queue: queue.Queue = queue.Queue()
        self._closing = False

    def submit(self, name: str, *args) -> None:
        self._queue.put((name, args))

    def shutdown(self) -> None:
        self._closing = True  # skip whatever is still queued
        self._queue.put(None)

    def run(self) -> None:
        client: ApiClient | None = None
        while True:
            item = self._queue.get()
            if item is None:
                break
            if self._closing:
                continue
            name, args = item
            if name == "connect":
                if client:
                    client.close()
                client = ApiClient(args[0])
                result = client.config()
            elif client is None:
                result = Result(False, error="not connected")
            else:
                result = getattr(client, name)(*args)  # e.g. client.drive("forward", 50)
            self.done.emit(name, args, result)
        if client:
            client.close()


class ScanThread(QThread):
    """One batch scan (snapshot over Wi-Fi + QR + classify + save), off the GUI thread.

    Separate from CommandThread, so a slow snapshot never delays a drive command or STOP.
    """
    scan_done = Signal(object, str)  # result dict (None on failure), error message

    def __init__(self, classifier, snapshot_url: str, climate: dict | None = None):
        super().__init__()
        self.classifier = classifier
        self.snapshot_url = snapshot_url
        self.climate = climate  # robot's climate at scan time (stored only if fresh)

    def run(self) -> None:
        try:
            result = scan_plant(self.classifier, snapshot_url=self.snapshot_url,
                                climate=self.climate)
        except ScanError as err:
            self.scan_done.emit(None, str(err))
        except Exception as err:  # e.g. a broken model file: show it, don't kill the thread silently
            traceback.print_exc()
            self.scan_done.emit(None, "scan failed: %s" % err)
        else:
            self.scan_done.emit(result, "")


class FeedbackThread(QThread):
    """Shows each batch scan result on the tank: an LED signal and beeps (SCAN_FEEDBACK), then
    after FEEDBACK_LED_S the colour of the current mode comes back.

    Own thread and own ApiClient, like ScanThread: it waits between beeps and for 5 s, which must
    never block the GUI or delay a drive command / STOP in CommandThread's queue.
    - Robot not reachable: the first request (status) fails and that result is skipped, silently.
    - An auto mode is running: beeps only, so the LED keeps showing the mode colours.
    - A newer result arrives: it replaces this one at once (no restore in between).
    """

    def __init__(self, make_client=ApiClient):
        super().__init__()
        self._queue: queue.Queue = queue.Queue()
        self._closing = False
        self._make_client = make_client  # tests pass a fake

    def show(self, address: str, status: str) -> None:
        """GUI thread: only queues the result, returns at once."""
        if status in SCAN_FEEDBACK:
            self._queue.put((address, status))

    def shutdown(self) -> None:
        self._closing = True  # a signal still on gets the mode colour back first
        self._queue.put(None)

    def run(self) -> None:
        client: ApiClient | None = None
        current = None
        while True:
            item = self._queue.get()
            if item is None or self._closing:
                break
            address, status = item
            if address != current:
                if client:
                    client.close()
                client, current = self._make_client(address, FEEDBACK_TIMEOUT_S), address
            self._show(client, status)
        if client:
            client.close()

    def _show(self, client: ApiClient, status: str) -> None:
        colour, blinks, beeps = SCAN_FEEDBACK[status]
        reply = client.status()
        if not reply.ok:
            return  # robot not reachable (or not connected yet): skip, silently
        # In an auto mode the LED shows the mode (blue, yellow = line lost): leave it, only beep.
        use_led = reply.data.get("mode", "manual") == "manual"
        # Replies from here on are not checked: a lost one only costs that part of the signal.
        restore_at = time.monotonic() + FEEDBACK_LED_S
        if use_led and not blinks:
            client.led(*colour)
        finished = self._beeps(client, beeps)
        if finished and use_led and blinks:
            finished = self._blink(client, colour, blinks)
        if finished and use_led:
            finished = self._wait(restore_at - time.monotonic())
        if use_led and (finished or self._closing):
            self._restore(client)

    def _beeps(self, client: ApiClient, count: int) -> bool:
        """count short beeps with a pause between them. False if interrupted."""
        for n in range(count):
            if n > 0 and not self._wait(FEEDBACK_BEEP_GAP_S):
                return False
            client.beep(FEEDBACK_BEEP_S)
        return True

    def _blink(self, client: ApiClient, colour: tuple, times: int) -> bool:
        """LED off, then colour, `times` times, FEEDBACK_BLINK_S each. False if interrupted.

        Timed from a running deadline: a slow request shortens the next wait, so the blink keeps
        its rhythm instead of stretching.
        """
        step = time.monotonic()
        for _ in range(times):
            for shade in (LED_OFF, colour):
                client.led(*shade)
                step += FEEDBACK_BLINK_S
                if not self._wait(step - time.monotonic()):
                    return False
        return True

    def _restore(self, client: ApiClient) -> None:
        """Back to the mode colour, in manual only. If an auto mode started meanwhile, that mode
        change already dropped our colour (the robot clears /api/led on a mode change)."""
        reply = client.status()
        if reply.ok and reply.data.get("mode", "manual") == "manual":
            client.led(*LED_MANUAL)

    def _wait(self, seconds: float) -> bool:
        """Sleep up to `seconds` in small steps. False at once when something new is queued
        (a newer result, or None = closing): the caller then stops its signal."""
        end = time.monotonic() + seconds
        while self._queue.empty():
            if time.monotonic() >= end:
                return True
            self.msleep(20)
        return False


if QWebEnginePage is not None:
    class CardPage(QWebEnginePage):
        """The card's buttons are links to ACTION_BASE + "save" / "scan" / "back". They are
        caught here and turned into a signal instead of being loaded as pages."""
        action = Signal(str)

        def acceptNavigationRequest(self, url, nav_type, is_main_frame) -> bool:
            if url.toString().startswith(ACTION_BASE):
                self.action.emit(url.toString()[len(ACTION_BASE):])
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class CardExporter(QObject):
    """Save / Share: renders the card (export mode: no buttons) in a hidden view as tall as the
    whole card, then saves a screenshot of it as PNG. Chromium draws asynchronously, hence the
    steps: load -> ask the page height -> resize -> wait a moment -> grab."""
    finished = Signal(str, str)  # PNG path ("" on failure), error message

    def __init__(self, html: str, base_url: QUrl, path: str, parent: QObject):
        super().__init__(parent)
        self.path = path
        self.view = QWebEngineView()
        self.view.setAttribute(Qt.WA_DontShowOnScreen)  # rendered, but never visible
        self.view.resize(CARD_WIDTH, CARD_HEIGHT)
        self.view.show()
        self.view.loadFinished.connect(self._loaded)
        self.view.setHtml(html, base_url)

    def _loaded(self, ok: bool) -> None:
        if not ok:
            self._done("", "could not render the card")
            return
        QTimer.singleShot(300, lambda: self.view.page().runJavaScript(
            "document.documentElement.scrollHeight", self._sized))

    def _sized(self, height) -> None:
        self.view.resize(CARD_WIDTH, int(height or CARD_HEIGHT))
        QTimer.singleShot(500, self._grab)

    def _grab(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if self.view.grab().save(self.path):
            self._done(self.path, "")
        else:
            self._done("", "could not write " + self.path)

    def _done(self, path: str, error: str) -> None:
        self.view.deleteLater()
        self.finished.emit(path, error)
        self.deleteLater()


def scan_summary(result: dict) -> str:
    """The Batch scan box line, e.g. 'last: T-02  Remove  (Spoiled tomato)'.

    Not sure / no tomato or pepper: the status words only. A name in brackets would repeat them
    ("Not sure - scan again  (not sure - scan again)"), and a guess is never shown as a diagnosis.
    Every other status comes from a condition in the knowledge base (classifier.status_for), so
    it has a name.
    """
    words = STATUS_TEXT[result["status"]]
    if result["status"] in ("uncertain", "no_produce"):
        return "last: %s  %s" % (result["plant_id"], words)
    return "last: %s  %s  (%s)" % (result["plant_id"], words, result["condition"]["name"])


def make_stub_banner(model_name: str) -> QLabel:
    """Big red "STUB - not using the camera image" label for the Batch scan box. Hidden unless
    the loaded classifier is the stub (it ignores the photo)."""
    banner = QLabel(STUB_BANNER)
    banner.setAlignment(Qt.AlignCenter)
    banner.setWordWrap(True)
    banner.setStyleSheet("background: #d61f1f; color: white; font-size: 16px; font-weight: bold;"
                         " padding: 6px; border-radius: 6px;")
    if not is_stub(model_name):
        banner.hide()  # (never setVisible(True) here: without a parent yet it would open a window)
    return banner


class ScanResultWindow(QWidget):
    """The last scan as a result card (scout/card.py + templates/card.html), the same card the
    field report's "View" links open. Wide window = two columns, narrow = phone layout.
    A separate window, reused for every scan.
    """
    scan_again = Signal()
    message = Signal(str)  # for the main window's status bar

    def __init__(self, parent: QWidget, db_path: str = scout_config.DB_PATH,
                 exports_dir: str = scout_config.EXPORTS_DIR):
        super().__init__(parent, Qt.Window)  # own window, but closes with the main window
        self.setWindowTitle("AgriScout.Zm - scan result")
        self.db_path = db_path
        self.exports_dir = exports_dir
        # Photos are stored as "scans/x.jpg", relative to the folder of the database.
        self.base_url = QUrl.fromLocalFile(os.path.dirname(os.path.abspath(db_path)) + os.sep)
        self.data: dict | None = None
        self.html = ""
        self.exporter: CardExporter | None = None

        self.page = CardPage(self)
        self.page.action.connect(self.on_action)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        # 90 % of the screen, centred: on a laptop/projector the card switches to its wide
        # two-column layout (card.html, >= 1000 px). Resizable, narrow = phone layout.
        area = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        self.resize(int(area.width() * WINDOW_SCREEN_SHARE),
                    int(area.height() * WINDOW_SCREEN_SHARE))
        self.move(area.center() - self.rect().center())

    def show_result(self, result: dict) -> None:
        conn = scout_db.open_db(self.db_path)
        try:
            self.data = card_data(conn, result["scan_id"])
        finally:
            conn.close()
        self.html = render_card(self.data, "app")
        self.view.setHtml(self.html, self.base_url)
        self.show()
        self.raise_()
        self.activateWindow()

    def on_action(self, action: str) -> None:
        if action == "back":
            self.hide()
        elif action == "scan":
            self.scan_again.emit()
        elif action == "save":
            self.save_png()

    def export_path(self) -> str:
        return os.path.join(self.exports_dir, export_name(self.data))

    def save_png(self) -> None:
        if self.data is None or self.exporter is not None:
            return  # nothing shown yet, or a save is still running
        self.exporter = CardExporter(render_card(self.data, "export"), self.base_url,
                                     self.export_path(), self)
        self.exporter.finished.connect(self.on_saved)

    def on_saved(self, path: str, error: str) -> None:
        self.exporter = None
        if error:
            self.message.emit("Save failed: " + error)
            return
        path = os.path.normpath(path)
        self.message.emit("Saved " + path)
        # Open Explorer with the PNG selected, ready to drag into WhatsApp / e-mail.
        subprocess.Popen('explorer /select,"%s"' % path)


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self, address: str):
        super().__init__()
        self.setWindowTitle("AgriScout.Zm")
        self.address = address
        self.config: dict = {}

        # Drive state. held_keys: drive keys held down, oldest first (last one wins).
        self.held_keys: list[str] = []
        self.button_cmd: str | None = None
        self.sent_cmd: str | None = None      # what we are currently sending ("stop" or a move)
        self.blocked_cmd: str | None = None   # got a 409 for this command: don't resend until released
        self.drives_pending = 0               # drive requests queued or on the way
        self.obstacle_msg = ""

        self.fail_count = 0
        self.sync_sliders = True  # copy the server's pan/tilt into the sliders on the next status
        self.last_mode = "manual"
        self.scan_rows: list[tuple[QLabel, QProgressBar]] = []
        # Newest climate from the robot + time.monotonic() when it arrived (None before the first)
        self.last_climate: tuple[Climate, float] | None = None

        self.video: VideoThread | None = None
        self.old_videos: list[VideoThread] = []  # stopping; kept alive until they finish

        # Batch scanning (scout/). A bad seed file or model must not stop the robot controls,
        # so a setup error only disables the Scan batch button.
        self.plant_scanner: ScanThread | None = None
        self.scan_window: ScanResultWindow | None = None
        self.classifier = None
        self.scan_setup_error = ""
        try:
            if WEBENGINE_ERROR:
                raise RuntimeError(WEBENGINE_ERROR)
            scout_db.setup()
            self.classifier = make_classifier()
        except Exception as err:
            traceback.print_exc()
            self.scan_setup_error = str(err)

        self.settings = open_settings()
        self.feedback_choice = load_feedback_choice(self.settings)

        self._build_ui()

        self.commands = CommandThread()
        self.commands.done.connect(self.on_command_done)
        self.commands.start()
        self.status_thread = StatusThread()
        self.status_thread.status.connect(self.on_status)
        self.status_thread.start()
        self.feedback = FeedbackThread()
        self.feedback.start()

        self.drive_timer = QTimer(self)
        self.drive_timer.setInterval(DRIVE_REPEAT_MS)
        self.drive_timer.timeout.connect(self.drive_tick)
        self.drive_timer.start()

        self.camera_timer = QTimer(self)
        self.camera_timer.setSingleShot(True)
        self.camera_timer.setInterval(CAMERA_DEBOUNCE_MS)
        self.camera_timer.timeout.connect(self.send_camera)

        self.front_timer = QTimer(self)
        self.front_timer.setSingleShot(True)
        self.front_timer.setInterval(CAMERA_DEBOUNCE_MS)
        self.front_timer.timeout.connect(
            lambda: self.commands.submit("front_servo", self.front_slider.value()))

        QApplication.instance().installEventFilter(self)
        self.connect_robot()

    # ---------- layout ----------

    def _build_ui(self) -> None:
        # Top: robot address
        self.address_box = QLineEdit(self.address)
        # Only focused when clicked: while it has focus, W/A/S/D type letters instead of driving.
        self.address_box.setFocusPolicy(Qt.ClickFocus)
        self.address_box.returnPressed.connect(self.connect_robot)
        connect_btn = QPushButton("Connect")
        connect_btn.setFocusPolicy(Qt.NoFocus)
        connect_btn.clicked.connect(self.connect_robot)
        top = QHBoxLayout()
        top.addWidget(QLabel("Robot:"))
        top.addWidget(self.address_box, 1)
        top.addWidget(connect_btn)

        # Left: video
        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumSize(320, 240)  # small: the control panel gets space first
        # Ignored: the label never asks to grow to the frame size (otherwise the window creeps).
        self.video_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.video_label.setStyleSheet("background: #222; color: #aaa; font-size: 20px;")
        self.show_no_video("connecting...")

        # Right: obstacle banner, drive pad, speed, camera, beep
        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background: #c00; color: white; font-size: 18px; "
                                  "font-weight: bold; padding: 10px; border-radius: 4px;")
        self.banner.hide()

        pad = QGridLayout()
        buttons = [("Spin L (Q)", "spin_left", 0, 0), ("Forward (W)", "forward", 0, 1),
                   ("Spin R (E)", "spin_right", 0, 2), ("Left (A)", "left", 1, 0),
                   ("Right (D)", "right", 1, 2), ("Back (S)", "back", 2, 1)]
        for text, cmd, row, col in buttons:
            btn = QPushButton(text)
            btn.setMinimumHeight(BIG_BUTTON_H)
            btn.setFocusPolicy(Qt.NoFocus)  # keep keyboard focus for W/A/S/D and Space
            btn.pressed.connect(lambda cmd=cmd: self.on_button(cmd))
            btn.released.connect(lambda: self.on_button(None))
            pad.addWidget(btn, row, col)
        stop_btn = QPushButton("STOP (Space)")
        stop_btn.setMinimumHeight(BIG_BUTTON_H)
        stop_btn.setFocusPolicy(Qt.NoFocus)
        stop_btn.setStyleSheet("background: #b00; color: white; font-weight: bold;")
        stop_btn.pressed.connect(self.stop_all)
        pad.addWidget(stop_btn, 1, 1)

        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(0, 100)
        self.speed_slider.setValue(50)
        self.speed_label = QLabel()
        self.speed_slider.valueChanged.connect(lambda v: self.speed_label.setText(f"Speed {v}"))
        self.speed_label.setText(f"Speed {self.speed_slider.value()}")
        drive_box = QGroupBox("Drive (hold)")
        drive_layout = QVBoxLayout(drive_box)
        drive_layout.addLayout(pad)
        drive_layout.addWidget(self.speed_label)
        drive_layout.addWidget(self.speed_slider)

        self.pan_slider = QSlider(Qt.Horizontal)
        self.tilt_slider = QSlider(Qt.Vertical)
        for slider in (self.pan_slider, self.tilt_slider):
            slider.setRange(0, 180)
            slider.setValue(90)
            slider.valueChanged.connect(lambda _: self.camera_timer.start())
        centre_btn = QPushButton("Centre")
        centre_btn.setFocusPolicy(Qt.NoFocus)
        centre_btn.clicked.connect(self.centre_camera)
        self.tilt_slider.setMaximumHeight(60)  # compact: the camera group is low priority
        tilt_col = QVBoxLayout()
        tilt_col.setSpacing(0)
        tilt_col.addWidget(QLabel("up"), 0, Qt.AlignHCenter)
        tilt_col.addWidget(self.tilt_slider, 0, Qt.AlignHCenter)
        tilt_col.addWidget(QLabel("down"), 0, Qt.AlignHCenter)
        pan_row = QHBoxLayout()
        pan_row.addWidget(QLabel("pan left"))
        pan_row.addWidget(self.pan_slider, 1)
        pan_row.addWidget(QLabel("right"))
        pan_col = QVBoxLayout()
        pan_col.addStretch(1)
        pan_col.addLayout(pan_row)
        pan_col.addWidget(centre_btn)
        pan_col.addStretch(1)
        camera_box = QGroupBox("Camera")
        camera_layout = QHBoxLayout(camera_box)
        camera_layout.addLayout(tilt_col)
        camera_layout.addLayout(pan_col, 1)

        # Ultrasonic: point it, big distance readout, scan
        self.distance_big = QLabel("-- cm")
        self.distance_big.setAlignment(Qt.AlignCenter)
        self.distance_big.setStyleSheet("font-size: 24px; font-weight: bold;")
        self.front_slider = QSlider(Qt.Horizontal)
        self.front_slider.setRange(0, 180)
        self.front_slider.setValue(90)
        self.front_slider.valueChanged.connect(lambda _: self.front_timer.start())
        front_centre_btn = QPushButton("Centre")
        front_centre_btn.setFocusPolicy(Qt.NoFocus)
        front_centre_btn.clicked.connect(self.centre_front)
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.setFocusPolicy(Qt.NoFocus)
        self.scan_btn.clicked.connect(self.start_scan)
        self.scan_grid = QGridLayout()
        self.scan_grid.setVerticalSpacing(2)
        ultra_box = QGroupBox("Ultrasonic")
        ultra_layout = QGridLayout(ultra_box)
        ultra_layout.addWidget(self.distance_big, 0, 0, 1, 4)
        ultra_layout.addWidget(QLabel("left"), 1, 0)
        ultra_layout.addWidget(self.front_slider, 1, 1, 1, 2)
        ultra_layout.addWidget(QLabel("right"), 1, 3)
        ultra_layout.addWidget(front_centre_btn, 2, 0, 1, 2)
        ultra_layout.addWidget(self.scan_btn, 2, 2, 1, 2)
        ultra_layout.addLayout(self.scan_grid, 3, 0, 1, 4)

        # Mode: one checked button = the mode the robot reports. Stacked, so it fits beside Drive.
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        mode_box = QGroupBox("Mode")
        mode_layout = QVBoxLayout(mode_box)
        for text, name in MODE_BUTTONS:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setMinimumHeight(BIG_BUTTON_H)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setProperty("mode", name)
            btn.clicked.connect(lambda _=False, name=name: self.commands.submit("mode", name))
            self.mode_group.addButton(btn)
            mode_layout.addWidget(btn)
            if name == "manual":
                btn.setChecked(True)
        mode_layout.addStretch(1)

        # LED colour buttons on one row + Beep (the robot shows state colours again after a
        # mode change)
        led_box = QGroupBox("LED / Beep")
        led_layout = QHBoxLayout(led_box)
        led_layout.setSpacing(4)
        for text, rgb in LED_BUTTONS:
            btn = QPushButton(text)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setStyleSheet("padding: 3px 6px;")
            btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            btn.clicked.connect(lambda _=False, rgb=rgb: self.commands.submit("led", *rgb))
            led_layout.addWidget(btn)
        beep_btn = QPushButton("Beep")
        beep_btn.setFocusPolicy(Qt.NoFocus)
        beep_btn.setStyleSheet("padding: 3px 8px; font-weight: bold;")
        beep_btn.clicked.connect(lambda: self.commands.submit("beep", 0.2))
        led_layout.addSpacing(8)
        led_layout.addWidget(beep_btn)

        # Batch scan: photo -> QR -> diagnosis (runs on this laptop, see scout/)
        self.plant_scan_btn = QPushButton("Scan batch")
        self.plant_scan_btn.setFocusPolicy(Qt.NoFocus)
        self.plant_scan_btn.setMinimumHeight(BIG_BUTTON_H)
        self.plant_scan_btn.setStyleSheet("font-weight: bold;")
        self.plant_scan_btn.clicked.connect(self.start_plant_scan)
        report_btn = QPushButton("Open field report")
        report_btn.setFocusPolicy(Qt.NoFocus)
        report_btn.setMinimumHeight(BIG_BUTTON_H)
        report_btn.clicked.connect(self.open_report)
        self.plant_scan_info = QLabel()
        self.plant_scan_info.setWordWrap(True)
        self.plant_scan_info.setStyleSheet("font-size: 11px;")
        if self.classifier is not None:
            self.plant_scan_info.setText("model: " + self.classifier.name)
        else:
            self.plant_scan_btn.setEnabled(False)
            self.plant_scan_info.setText("scan disabled: " + self.scan_setup_error)
        self.stub_banner = make_stub_banner(self.classifier.name if self.classifier else "")
        # Robot lights & beeps after a scan: Off / Problems only / Every scan (remembered).
        self.feedback_combo = QComboBox()
        self.feedback_combo.setFocusPolicy(Qt.NoFocus)  # W/A/S/D and Space keep driving
        for text, cid in FEEDBACK_CHOICES:
            self.feedback_combo.addItem(text, cid)
        self.feedback_combo.setCurrentIndex(self.feedback_combo.findData(self.feedback_choice))
        self.feedback_combo.currentIndexChanged.connect(self.on_feedback_choice)
        plant_box = QGroupBox("Batch scan")
        plant_layout = QGridLayout(plant_box)
        plant_layout.addWidget(self.stub_banner, 0, 0, 1, 2)
        plant_layout.addWidget(self.plant_scan_btn, 1, 0)
        plant_layout.addWidget(report_btn, 1, 1)
        plant_layout.addWidget(QLabel("Robot lights & beeps after a scan:"), 2, 0)
        plant_layout.addWidget(self.feedback_combo, 2, 1)
        plant_layout.addWidget(self.plant_scan_info, 3, 0, 1, 2)

        # Climate: the robot's DHT11 (it updates every 5 s on the robot; we see it with each status)
        self.climate_label = QLabel()
        self.climate_label.setWordWrap(True)
        climate_refresh = QPushButton("Refresh")
        climate_refresh.setFocusPolicy(Qt.NoFocus)
        climate_refresh.clicked.connect(lambda: self.commands.submit("status"))
        climate_box = QGroupBox("Climate")
        climate_layout = QHBoxLayout(climate_box)
        climate_layout.addWidget(self.climate_label, 1)
        climate_layout.addWidget(climate_refresh)
        self.show_climate(Climate(False, reason="not connected yet"))

        # Soil: probe on the Uno next to the robot (Pi USB). Information only.
        self.soil_label = QLabel()
        self.soil_label.setWordWrap(True)
        soil_box = QGroupBox("Soil")
        QHBoxLayout(soil_box).addWidget(self.soil_label)
        self.show_soil(Soil(False, reason="not connected yet"))
        climate_soil = QHBoxLayout()
        climate_soil.setContentsMargins(0, 0, 0, 0)
        climate_soil.addWidget(climate_box, 1)
        climate_soil.addWidget(soil_box, 1)

        # Right panel = two parts:
        #   pinned (never scrolls): obstacle banner, Mode + Drive side by side (with STOP)
        #   scroll (vertical only, if the screen is short): Climate | Soil, Batch scan, Ultrasonic,
        #   Camera, LED / Beep
        mode_drive = QHBoxLayout()
        mode_drive.setContentsMargins(0, 0, 0, 0)
        mode_drive.addWidget(mode_box)
        mode_drive.addWidget(drive_box, 1)
        pinned_layout = QVBoxLayout()
        pinned_layout.setContentsMargins(0, 0, 0, 0)
        pinned_layout.addWidget(self.banner)
        pinned_layout.addLayout(mode_drive)
        self.pinned = QWidget()
        self.pinned.setLayout(pinned_layout)

        scroll_layout = QVBoxLayout()
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.addLayout(climate_soil)
        scroll_layout.addWidget(plant_box)
        scroll_layout.addWidget(ultra_box)
        scroll_layout.addWidget(camera_box)
        scroll_layout.addWidget(led_box)
        scroll_layout.addStretch(1)
        self.scroll_content = QWidget()
        self.scroll_content.setLayout(scroll_layout)
        self.right_scroll = QScrollArea()
        self.right_scroll.setWidget(self.scroll_content)
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setFrameShape(QFrame.NoFrame)
        self.right_scroll.setFocusPolicy(Qt.NoFocus)
        self.right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # never clip sideways

        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.pinned)
        right_layout.addWidget(self.right_scroll, 1)
        self.right_panel = QWidget()
        self.right_panel.setLayout(right_layout)

        middle = QHBoxLayout()
        middle.addWidget(self.video_label, 1)  # video takes whatever width is left
        middle.addWidget(self.right_panel)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addLayout(middle, 1)
        central = QWidget()
        central.setLayout(root)
        # Clicking anywhere in the window (video, empty space) takes focus away from the address box.
        central.setFocusPolicy(Qt.ClickFocus)
        self.setCentralWidget(central)

        # Bottom: status bar
        self.conn_label = QLabel()
        self.distance_label = QLabel("distance: --")
        self.line_label = QLabel("line: --")
        self.cmd_label = QLabel("cmd: --")
        self.mode_label = QLabel("mode: --")
        bar = self.statusBar()
        for label in (self.conn_label, self.mode_label, self.distance_label, self.line_label,
                      self.cmd_label):
            bar.addPermanentWidget(label)
        self.set_connected(False)
        self.fit_panel_width()
        self.resize(1150, 720)

    def fit_panel_width(self) -> None:
        """Make the right panel exactly as wide as its widest group, plus room for the vertical
        scrollbar. So nothing is ever cut off on the right; the video gets the rest."""
        scrollbar = self.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        widest = max(self.pinned.sizeHint().width(), self.scroll_content.sizeHint().width())
        self.right_panel.setFixedWidth(widest + scrollbar + 4)

    # ---------- connection ----------

    def connect_robot(self) -> None:
        self.address = self.address_box.text().strip() or DEFAULT_ADDRESS
        self.fail_count = 0
        self.sync_sliders = True
        self.centralWidget().setFocus()  # leave the address box so the drive keys work
        self.statusBar().showMessage(f"connecting to {self.address}...")
        self.commands.submit("connect", self.address)
        self.status_thread.set_address(self.address)

    def apply_config(self, config: dict) -> None:
        self.config = config
        servo, limits = config["servo"], config["limits"]

        lo, hi = limits["speed"]
        self.speed_slider.setRange(lo, hi)
        self.speed_slider.setValue(config["default_speed"])

        # "Left means left": the slider's left end must be the angle that looks left.
        # Qt puts the minimum on the left (horizontal) / bottom (vertical) unless inverted.
        self.pan_slider.blockSignals(True)
        self.tilt_slider.blockSignals(True)
        self.pan_slider.setRange(*limits["pan"])
        self.pan_slider.setInvertedAppearance(servo["PAN_LEFT"] > servo["PAN_RIGHT"])
        self.tilt_slider.setRange(*limits["tilt"])
        self.tilt_slider.setInvertedAppearance(servo["TILT_UP"] < servo["TILT_DOWN"])
        self.pan_slider.blockSignals(False)
        self.tilt_slider.blockSignals(False)

        # Ultrasonic: FRONT is mirrored vs pan on this tank; the constants handle it.
        self.front_slider.blockSignals(True)
        self.front_slider.setRange(*limits["front"])
        self.front_slider.setInvertedAppearance(servo["FRONT_LEFT"] > servo["FRONT_RIGHT"])
        self.front_slider.blockSignals(False)
        self.build_scan_rows(config.get("scan_angles", []), servo)

        self.start_video(config["camera_stream_url"])
        self.statusBar().showMessage(f"connected to {self.address}", 3000)

    # ---------- video ----------

    def start_video(self, url: str) -> None:
        if self.video:
            self.video.requestInterruption()  # it may be blocked in a read for up to 3 s,
            self.old_videos.append(self.video)  # so don't wait here; keep it alive until it ends
        self.old_videos = [t for t in self.old_videos if t.isRunning()]
        self.video = VideoThread(url)
        self.video.frame_ready.connect(self.on_frame)
        self.video.no_video.connect(self.on_no_video)
        self.video.start()

    def on_frame(self, thread: VideoThread) -> None:
        if thread is not self.video:
            return
        image = thread.take_frame()
        if image is None:
            return
        pixmap = QPixmap.fromImage(image).scaled(self.video_label.size(), Qt.KeepAspectRatio,
                                                 Qt.SmoothTransformation)
        self.video_label.setPixmap(pixmap)

    def on_no_video(self, thread: VideoThread, reason: str) -> None:
        if thread is self.video:
            self.show_no_video(reason)

    def show_no_video(self, reason: str) -> None:
        self.video_label.clear()
        self.video_label.setText("NO VIDEO\n\n" + reason)

    # ---------- driving ----------

    def held_cmd(self) -> str | None:
        """The move the user is holding right now (a button beats keys; newest key wins)."""
        if self.button_cmd:
            return self.button_cmd
        return self.held_keys[-1] if self.held_keys else None

    def on_button(self, cmd: str | None) -> None:
        self.button_cmd = cmd
        self.update_drive()

    def update_drive(self) -> None:
        """Called when what is held changes: react now, don't wait for the next timer tick."""
        cmd = self.held_cmd()
        if cmd != self.blocked_cmd:
            self.blocked_cmd = None  # a new press may try again
        if cmd is None:
            if self.sent_cmd != "stop":
                self.send_drive("stop")
        elif cmd != self.sent_cmd:
            self.send_drive(cmd)

    def drive_tick(self) -> None:
        """Every 150 ms: repeat the held move, so the server's 0.8 s watchdog stays happy."""
        cmd = self.held_cmd()
        if cmd is None or cmd == self.blocked_cmd:
            return
        if self.drives_pending == 0:  # never queue a 2nd one: then STOP waits at most 1 request
            self.send_drive(cmd)

    def send_drive(self, cmd: str) -> None:
        self.sent_cmd = cmd
        self.drives_pending += 1
        self.commands.submit("drive", cmd, self.speed_slider.value())

    def stop_all(self) -> None:
        self.held_keys.clear()
        self.button_cmd = None
        self.send_drive("stop")

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.KeyPress, QEvent.KeyRelease):
            if isinstance(QApplication.focusWidget(), QLineEdit):
                return False  # typing an address: W/A/S/D are letters here
            key = event.key()
            if key == Qt.Key_Space:
                if event.type() == QEvent.KeyPress and not event.isAutoRepeat():
                    self.stop_all()
                return True
            cmd = KEY_TO_CMD.get(key)
            if cmd is None:
                return False
            if not event.isAutoRepeat():  # Windows repeats KeyPress while held: ignore those
                if event.type() == QEvent.KeyPress and cmd not in self.held_keys:
                    self.held_keys.append(cmd)
                elif event.type() == QEvent.KeyRelease and cmd in self.held_keys:
                    self.held_keys.remove(cmd)
                self.update_drive()
            return True
        return super().eventFilter(obj, event)

    def changeEvent(self, event: QEvent) -> None:
        # Alt-Tab while holding W: the key release goes to another window, so we'd never see it.
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow():
            if self.held_cmd():
                self.stop_all()
        super().changeEvent(event)

    # ---------- camera ----------

    def send_camera(self) -> None:
        self.commands.submit("camera", self.pan_slider.value(), self.tilt_slider.value())

    def centre_camera(self) -> None:
        servo = self.config.get("servo", {"PAN_CENTER": 90, "TILT_LEVEL": 90})
        self.pan_slider.setValue(servo["PAN_CENTER"])  # valueChanged -> camera_timer -> send
        self.tilt_slider.setValue(servo["TILT_LEVEL"])

    # ---------- ultrasonic ----------

    def centre_front(self) -> None:
        centre = self.config.get("servo", {}).get("FRONT_CENTER", 90)
        self.front_slider.setValue(centre)  # valueChanged -> front_timer -> send

    def build_scan_rows(self, angles: list, servo: dict) -> None:
        """One row per scan angle, in the order the server scans them (left -> right)."""
        while self.scan_grid.count():
            self.scan_grid.takeAt(0).widget().deleteLater()
        names = {servo["FRONT_LEFT"]: "Left", servo["FRONT_CENTER"]: "Centre",
                 servo["FRONT_RIGHT"]: "Right"}
        self.scan_rows = []
        for row, angle in enumerate(angles):
            label = QLabel(names.get(angle, f"{angle}°"))
            bar = QProgressBar()
            bar.setRange(0, SCAN_BAR_MAX_CM)
            bar.setValue(0)
            bar.setFormat("--")
            bar.setMaximumHeight(16)  # thin rows: 5 of them fit in little height
            bar.setStyleSheet("font-size: 11px;")
            label.setStyleSheet("font-size: 11px;")
            self.scan_grid.addWidget(label, row, 0)
            self.scan_grid.addWidget(bar, row, 1)
            self.scan_rows.append((label, bar))
        self.fit_panel_width()

    def start_scan(self) -> None:
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Scanning...")
        self.commands.submit("scan")

    def show_scan(self, distances: list) -> None:
        for (_, bar), cm in zip(self.scan_rows, distances):
            if cm is None:
                bar.setValue(0)
                bar.setFormat("no echo")
            else:
                bar.setValue(min(int(cm), SCAN_BAR_MAX_CM))
                bar.setFormat(f"{cm:.0f} cm")

    # ---------- batch scan ----------

    def snapshot_url(self) -> str:
        """mjpg-streamer's single-JPEG URL: same server as the video, action=snapshot."""
        stream = self.config.get("camera_stream_url")
        if stream and "action=stream" in stream:
            return stream.replace("action=stream", "action=snapshot")
        return scout_config.snapshot_url_for(self.address)  # not connected yet: guess from address

    def start_plant_scan(self) -> None:
        if self.classifier is None or (self.plant_scanner and self.plant_scanner.isRunning()):
            return
        self.plant_scan_btn.setEnabled(False)
        self.plant_scan_btn.setText("Scanning...")
        self.plant_scanner = ScanThread(self.classifier, self.snapshot_url(),
                                        climate_for_scan(self.last_climate, time.monotonic()))
        self.plant_scanner.scan_done.connect(self.on_plant_scan_done)
        self.plant_scanner.start()

    def on_plant_scan_done(self, result: dict | None, error: str) -> None:
        self.plant_scan_btn.setEnabled(True)
        self.plant_scan_btn.setText("Scan batch")
        if result is None:
            self.plant_scan_info.setText("scan failed: " + error)
            self.statusBar().showMessage("Batch scan failed: " + error, 6000)
            return
        self.plant_scan_info.setText(scan_summary(result))
        self.send_scan_feedback(result["status"])  # LED + beeps on the tank, if turned on
        if self.scan_window is None:
            self.scan_window = ScanResultWindow(self)
            self.scan_window.scan_again.connect(self.start_plant_scan)
            self.scan_window.message.connect(lambda text: self.statusBar().showMessage(text, 8000))
        self.scan_window.show_result(result)

    def send_scan_feedback(self, status: str) -> None:
        """Off: nothing at all goes to the robot, so its LED keeps the mode colour."""
        if feedback_wanted(self.feedback_choice, status):
            self.feedback.show(self.address, status)

    def on_feedback_choice(self, index: int) -> None:
        self.feedback_choice = self.feedback_combo.itemData(index)
        save_feedback_choice(self.settings, self.feedback_choice)

    def open_report(self) -> None:
        try:
            path = write_report()
        except Exception as err:
            traceback.print_exc()
            self.statusBar().showMessage("Report failed: %s" % err, 6000)
            return
        webbrowser.open(Path(path).as_uri())

    # ---------- replies from the threads ----------

    def on_command_done(self, name: str, args: tuple, result: Result) -> None:
        if name == "connect":
            if result.ok:
                self.apply_config(result.data)
            else:
                self.statusBar().showMessage(f"connect failed: {result.error}")
                self.show_no_video("not connected")
            return
        if name == "drive":
            self.drives_pending -= 1
            if result.status == 409 and "obstacle" in (result.error or ""):
                self.blocked_cmd = args[0]  # server refused to drive forward
                self.sent_cmd = "stop"
                self.show_obstacle(result.error)
                return
            if result.ok and result.data.get("recentred"):
                self.statusBar().showMessage(
                    "Ultrasonic re-centred: driving forward needs it looking ahead", 4000)
        if name == "status" and result.ok:  # Climate panel's Refresh button
            self.update_climate(parse_climate(result.data))
            self.show_soil(parse_soil(result.data))
        if name == "scan":
            self.scan_btn.setEnabled(True)
            self.scan_btn.setText("Scan")
            if result.ok:
                self.show_scan(result.data["distances_cm"])
        if not result.ok:
            self.statusBar().showMessage(f"{name} failed: {result.error}", 3000)

    def on_status(self, address: str, result: Result) -> None:
        if address != self.address:
            return  # reply from before the user changed the address
        if not result.ok:
            self.fail_count += 1
            if self.fail_count >= FAILS_BEFORE_RED:
                self.set_connected(False, result.error)
                self.show_climate(Climate(False, reason="robot not reachable"))
                self.show_soil(Soil(False, reason="robot not reachable"))
            return
        self.fail_count = 0
        self.set_connected(True)
        data = result.data
        self.update_climate(parse_climate(data))
        self.show_soil(parse_soil(data))

        distance = data.get("distance_cm")
        self.distance_label.setText("distance: --" if distance is None
                                    else f"distance: {distance:.0f} cm")
        side = "" if data.get("front_ahead", True) else "  (side)"
        self.distance_big.setText(("no echo" if distance is None else f"{distance:.0f} cm") + side)

        # Ultrasonic slider follows the robot (e.g. after forward re-centred it),
        # unless the user is dragging it or a move is about to be sent.
        if not self.front_slider.isSliderDown() and not self.front_timer.isActive():
            self.front_slider.blockSignals(True)
            self.front_slider.setValue(int(round(data.get("front", self.front_slider.value()))))
            self.front_slider.blockSignals(False)

        mode = data.get("mode", "manual")
        self.mode_label.setText(f"mode: {mode}")
        for btn in self.mode_group.buttons():
            if btn.property("mode") == mode and not btn.isChecked():
                btn.setChecked(True)
        if self.last_mode != "manual" and mode == "manual" and data.get("mode_reason"):
            self.statusBar().showMessage(f"Auto ended: {data['mode_reason']}", 5000)
        self.last_mode = mode
        line = data.get("line")
        if line:
            # ■ = sensor sees black (True), □ = white
            marks = "  ".join(f"{name} {'■' if line[name] else '□'}"
                              for name in ("L1", "L2", "R1", "R2"))
            self.line_label.setText("line: " + marks)
        self.cmd_label.setText(f"cmd: {data.get('cmd')} {data.get('speed')}")

        if data.get("obstacle"):
            self.show_obstacle(self.obstacle_msg or "OBSTACLE AHEAD: forward blocked")
        else:
            self.obstacle_msg = ""
            self.banner.hide()

        if self.sync_sliders:
            self.sync_sliders = False
            for slider, key in ((self.pan_slider, "pan"), (self.tilt_slider, "tilt")):
                slider.blockSignals(True)  # don't send the value straight back
                slider.setValue(int(round(data.get(key, slider.value()))))
                slider.blockSignals(False)

    def update_climate(self, climate: Climate) -> None:
        """A new climate from the robot: remember it (with when it came) and show it."""
        self.last_climate = (climate, time.monotonic())
        self.show_climate(climate)

    def show_climate(self, climate: Climate) -> None:
        text, state = climate_text(climate)
        self.climate_label.setText(text)
        self.climate_label.setStyleSheet("" if state == "fresh" else CLIMATE_GREY)

    def show_soil(self, soil: Soil) -> None:
        text, state = soil_text(soil)
        self.soil_label.setText(text)
        self.soil_label.setStyleSheet("" if state == "fresh" else CLIMATE_GREY)

    def show_obstacle(self, message: str) -> None:
        self.obstacle_msg = message
        self.banner.setText(message)
        self.banner.show()

    def set_connected(self, ok: bool, error: str | None = None) -> None:
        color, text = ("#1a1", "connected") if ok else ("#c00", "disconnected")
        self.conn_label.setText(f"<span style='color:{color}; font-size:16px'>●</span> {text}")
        self.conn_label.setToolTip(error or "")

    # ---------- closing ----------

    def closeEvent(self, event) -> None:
        self.drive_timer.stop()
        self.held_keys.clear()
        self.button_cmd = None
        # 1. Stop the command thread first (drops queued moves), so no old "forward"
        #    can reach the robot after our stop.
        self.commands.shutdown()
        self.commands.wait(2000)
        # 2. Send stop directly (at most 0.5 s). The server watchdog is the backup if this fails.
        client = ApiClient(self.address)
        client.drive("stop")
        client.close()
        # 3. Stop the other threads. A scan signal still on the LED first gets the mode colour
        #    back (FeedbackThread: the request under way + status + led, each <= its timeout).
        self.feedback.shutdown()
        threads = [self.status_thread] + self.old_videos + ([self.video] if self.video else [])
        for thread in threads:
            thread.requestInterruption()
        for thread in threads:
            thread.wait(VIDEO_TIMEOUT_MS + 500)
        self.feedback.wait(int(3 * FEEDBACK_TIMEOUT_S * 1000) + 1000)
        # 4. A batch scan can't be interrupted, but it ends within the snapshot timeout.
        if self.plant_scanner:
            self.plant_scanner.wait(int(scout_config.SNAPSHOT_TIMEOUT_S * 1000) + 2000)
        super().closeEvent(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="AgriScout desktop GUI")
    parser.add_argument("--address", default=DEFAULT_ADDRESS,
                        help=f"robot host:port (default {DEFAULT_ADDRESS})")
    args = parser.parse_args()
    try:
        # Own taskbar identity: otherwise Windows groups us under pythonw.exe's Python icon.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AgriScout.GUI")
    except (AttributeError, OSError):
        pass
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(ICON_PATH))
    window = MainWindow(args.address)
    window.showMaximized()  # biggest video on any screen (1920x1080 or a 1366x768 projector)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
