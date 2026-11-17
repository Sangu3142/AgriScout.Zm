"""desktop/crash_log.py: errors under pythonw (no console) must land in a log file."""

import sys
import threading

import pytest

import crash_log


@pytest.fixture
def log(tmp_path, monkeypatch):
    # Put back what install() replaces, so pytest keeps its own hooks.
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    boxes = []
    monkeypatch.setattr(crash_log, "_box", boxes.append)
    path = tmp_path / "AgriScout" / "gui.log"
    crash_log.install(str(path), show_box=False)
    yield path, boxes
    crash_log._log.close()


def raise_and_hook(exc):
    try:
        raise exc
    except Exception:
        sys.excepthook(*sys.exc_info())


def test_start_line_written(log):
    path, _ = log
    assert "AgriScout GUI started" in path.read_text(encoding="utf-8")


def test_uncaught_error_goes_to_log_and_box(log):
    path, boxes = log
    raise_and_hook(ValueError("servo exploded"))
    text = path.read_text(encoding="utf-8")
    assert "ValueError: servo exploded" in text and "Traceback" in text
    assert len(boxes) == 1 and "servo exploded" in boxes[0] and str(path) in boxes[0]


def test_thread_error_goes_to_log(log):
    path, _ = log

    def boom():
        raise RuntimeError("worker died")
    t = threading.Thread(target=boom, name="video")
    t.start()
    t.join()
    text = path.read_text(encoding="utf-8")
    assert "error in video" in text and "RuntimeError: worker died" in text


def test_big_log_is_rotated(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    path = tmp_path / "gui.log"
    path.write_text("x" * (crash_log.MAX_BYTES + 10), encoding="utf-8")
    crash_log.install(str(path), show_box=False)
    crash_log._log.close()
    assert (tmp_path / "gui.log.1").stat().st_size > crash_log.MAX_BYTES
    assert path.stat().st_size < 1000   # fresh log
