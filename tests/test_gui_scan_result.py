"""Scan result window (desktop/gui.py): shows the result card, its buttons work.

Runs without a screen: Qt's "offscreen" platform, set before the QApplication is created.
The PNG export itself (Chromium drawing) is checked by hand: it needs a real GPU/screen.
"""

import os

import pytest
from markupsafe import escape

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import gui  # noqa: E402
from scout import card, config  # noqa: E402
from scout.classifier import StubClassifier  # noqa: E402
from scout.pipeline import scan_plant  # noqa: E402
from test_scout_pipeline import photo  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def scan(scout_db, tmp_path):
    """Real scan_plant() results (stub), so the dict is exactly what the GUI gets."""
    stub = StubClassifier.from_file(config.STUB_SCRIPT)
    return lambda plant_id: scan_plant(stub, scout_db, image_bytes=photo(plant_id),
                                       scans_dir=str(tmp_path / "scans"))


@pytest.fixture
def window(app, scout_db, tmp_path):
    parent = QWidget()
    win = gui.ScanResultWindow(parent, db_path=scout_db, exports_dir=str(tmp_path / "exports"))
    yield win
    win.close()
    parent.deleteLater()


def test_webengine_is_available():
    assert gui.WEBENGINE_ERROR == "" and gui.QWebEngineView is not None


def test_window_opens_at_90_percent_of_the_screen_centred(window):
    area = window.screen().availableGeometry()
    assert window.width() == int(area.width() * 0.9)
    assert window.height() == int(area.height() * 0.9)
    assert abs(window.geometry().center().x() - area.center().x()) <= 1
    assert abs(window.geometry().center().y() - area.center().y()) <= 1


def test_uncertain_scan_shows_not_sure_card(window, scan):
    window.show_result(scan("T-03"))                        # spoiled 52 % -> uncertain
    assert window.data["status"] == "uncertain"
    assert "<h2>Not sure - scan again</h2>" in window.html
    assert "best guess: Spoiled tomato 52 %" in window.html
    assert card.UNCERTAIN_ADVICE in window.html and "Do now" not in window.html


def test_confident_scan_reuses_window(window, scan):
    window.show_result(scan("T-03"))
    window.show_result(scan("T-02"))                        # spoiled 88 % -> Remove
    assert window.data["status"] == "action"
    assert "<h2>Spoiled tomato</h2>" in window.html and "Do now" in window.html
    assert card.UNCERTAIN_ADVICE not in window.html
    # photos are "scans/x.jpg", relative to the database folder = the page's base URL
    assert window.base_url.toLocalFile().rstrip("/\\").endswith(os.path.basename(
        os.path.dirname(window.db_path)))


def test_card_buttons(window, scan, monkeypatch):
    window.show_result(scan("T-02"))
    events = []
    window.scan_again.connect(lambda: events.append("scan"))
    monkeypatch.setattr(window, "save_png", lambda: events.append("save"))
    window.on_action("scan")
    window.on_action("save")
    window.on_action("nonsense")                            # ignored
    assert events == ["scan", "save"]
    window.show()
    window.on_action("back")
    assert window.isHidden()


def test_page_turns_action_links_into_signals(window):
    from PySide6.QtCore import QUrl
    seen = []
    window.page.action.connect(seen.append)
    accepted = window.page.acceptNavigationRequest(
        QUrl(card.ACTION_BASE + "save"), gui.QWebEnginePage.NavigationType.NavigationTypeLinkClicked,
        True)
    assert accepted is False and seen == ["save"]


def test_stub_card_has_the_stub_banner(window, scan):
    window.show_result(scan("T-02"))
    assert card.STUB_BANNER in window.html and "Model: stub (scripted demo)" in window.html


def test_no_produce_card_in_the_window(window, scout_db, tmp_path):
    from test_scout_pipeline import FixedClassifier
    result = scan_plant(FixedClassifier([("background", 0.9)]), scout_db,
                        image_bytes=photo("T-01"), scans_dir=str(tmp_path / "scans"))
    window.show_result(result)
    assert window.data["status"] == "no_produce"
    assert "<h2>No tomato or green pepper found</h2>" in window.html
    assert str(escape(card.NO_PRODUCE_TEXT)) in window.html and "Do now" not in window.html


def test_stub_banner_widget(app):
    parent = QWidget()                                      # like the Batch scan box

    def banner_for(model_name):
        banner = gui.make_stub_banner(model_name)
        banner.setParent(parent)
        return banner

    stub = banner_for(StubClassifier().name)
    assert stub.text() == "STUB - not using the camera image"
    assert stub.isVisibleTo(parent)                         # shown with the box, for the stub
    assert not banner_for("onnx:plant_model.onnx").isVisibleTo(parent)
    assert not banner_for("").isVisibleTo(parent)           # scanning disabled: no classifier
    parent.deleteLater()


def test_export_path(window, scan, tmp_path):
    window.show_result(scan("T-02"))
    path = window.export_path()
    assert os.path.dirname(path) == str(tmp_path / "exports")
    assert os.path.basename(path).startswith("T-02_") and path.endswith(".png")
