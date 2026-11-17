"""Phase 5c: training photo capture (scout/capture.py) and the dataset zip. No camera needed."""

import datetime
import os
import zipfile

import pytest

from scout import capture, zip_dataset
from scout.classifier import MODEL_CLASSES
from scout.pipeline import ScanError

NOW = datetime.datetime(2026, 9, 28, 15, 30, 12, 345678)


def test_photo_path_naming(tmp_path):
    path = capture.photo_path(str(tmp_path), "tomato_fresh", NOW)
    assert path == os.path.join(str(tmp_path), "tomato_fresh", "20260928-153012-345.jpg")


def test_photo_path_never_overwrites(tmp_path):
    first = capture.photo_path(str(tmp_path), "pepper_spoiled", NOW)
    os.makedirs(os.path.dirname(first))
    open(first, "wb").close()
    second = capture.photo_path(str(tmp_path), "pepper_spoiled", NOW)
    assert second.endswith("20260928-153012-345_2.jpg")
    open(second, "wb").close()
    assert capture.photo_path(str(tmp_path), "pepper_spoiled", NOW).endswith("_3.jpg")


def test_class_must_be_a_model_class():
    assert capture.parse_args(["--class", "pepper_fresh"]).cls == "pepper_fresh"
    with pytest.raises(SystemExit):
        capture.parse_args(["--class", "maize_healthy"])
    with pytest.raises(SystemExit):
        capture.parse_args([])                               # --class is required


def test_saver_writes_snapshot_bytes_unchanged(tmp_path):
    saver = capture.Saver(str(tmp_path), "tomato_fresh", lambda: b"JPEG-BYTES")
    path = saver.save_one()
    assert open(path, "rb").read() == b"JPEG-BYTES"
    assert saver.saved == 1 and capture.count_photos(str(tmp_path / "tomato_fresh")) == 1


def test_saver_reports_a_failed_snapshot(tmp_path):
    def no_camera():
        raise ScanError("no camera snapshot from x (cannot connect)")
    saver = capture.Saver(str(tmp_path), "tomato_fresh", no_camera)
    assert saver.save_one() is None
    assert saver.saved == 0 and "cannot connect" in saver.message


def test_burst_saves_count_photos(tmp_path):
    saver = capture.Saver(str(tmp_path), "tomato_spoiled", lambda: b"x")
    saver.burst(count=4, gap_s=0.0)
    assert saver.saved == 4 and saver.burst_done == 0
    assert capture.count_photos(str(tmp_path / "tomato_spoiled")) == 4


def test_one_job_at_a_time(tmp_path):
    saver = capture.Saver(str(tmp_path), "tomato_fresh", lambda: b"x")
    assert saver.start(lambda: saver.burst(count=3, gap_s=0.05))
    assert not saver.start(saver.save_one)                   # burst still running
    saver.wait()
    assert saver.saved == 3 and not saver.busy


def test_overlay_without_video_is_a_black_frame():
    view = capture.draw_overlay(None, ["class: tomato_fresh"])
    assert view.shape == (360, capture.PREVIEW_WIDTH, 3)


def test_zip_dataset(tmp_path):
    data = tmp_path / "dataset"
    for cls, n in (("tomato_fresh", 3), ("pepper_spoiled", 1), ("background", 1)):
        (data / cls).mkdir(parents=True)
        for i in range(n):
            (data / cls / ("%d.jpg" % i)).write_bytes(b"jpg")
    (data / "tomato_fresh" / "notes.txt").write_text("not a photo")
    (data / "maize_healthy").mkdir()                         # not a model class: left out
    (data / "maize_healthy" / "0.jpg").write_bytes(b"jpg")

    out = tmp_path / "dataset.zip"
    assert zip_dataset.zip_dataset(str(data), str(out)) == {"tomato_fresh": 3, "tomato_spoiled": 0, "pepper_fresh": 0,
                                       "pepper_spoiled": 1, "background": 1}
    with zipfile.ZipFile(out) as z:
        names = sorted(z.namelist())
        # ImageFolder (Colab) order = alphabetical, not the MODEL_CLASSES order
        assert z.read("labels.txt").decode().split() == [
            "background", "pepper_fresh", "pepper_spoiled", "tomato_fresh", "tomato_spoiled"]
    assert names == ["dataset/background/0.jpg", "dataset/pepper_spoiled/0.jpg",
                     "dataset/tomato_fresh/0.jpg", "dataset/tomato_fresh/1.jpg",
                     "dataset/tomato_fresh/2.jpg", "labels.txt"]


def test_background_is_a_capture_class(tmp_path):
    assert capture.parse_args(["--class", "background"]).cls == "background"
    saver = capture.Saver(str(tmp_path), "background", lambda: b"EMPTY-TRAY")
    path = saver.save_one()
    assert os.path.dirname(path) == str(tmp_path / "background")


def test_capture_commands_for_all_five_classes():
    assert capture.capture_commands() == [
        "python -m scout.capture --class tomato_fresh",
        "python -m scout.capture --class tomato_spoiled",
        "python -m scout.capture --class pepper_fresh",
        "python -m scout.capture --class pepper_spoiled",
        "python -m scout.capture --class background",
    ]
    assert capture.capture_commands("10.0.0.7")[4] == (
        "python -m scout.capture --class background --robot 10.0.0.7")
    assert len(capture.capture_commands()) == len(MODEL_CLASSES)
