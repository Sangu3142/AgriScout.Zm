"""Phase 5a/5c: scan pipeline with a fake snapshot and the stub classifier."""

import datetime
import os

import cv2
import numpy as np
import pytest
import requests

from scout import config, db, pipeline
from scout.classifier import Classifier, StubClassifier
from scout.pipeline import ScanError, scan_plant
from scout.qr import make_payload, make_qr

NOW = datetime.datetime(2026, 9, 27, 14, 30, 12)


def photo(plant_id=None, brightness=140) -> bytes:
    """A JPEG like mjpg-streamer sends: 640x480, optionally with a QR stake in it."""
    frame = np.full((480, 640), brightness, np.uint8)
    if plant_id:
        qr = make_qr(make_payload(plant_id), 200)
        frame[100:100 + qr.shape[0], 200:200 + qr.shape[1]] = qr
    ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
    assert ok
    return jpeg.tobytes()


@pytest.fixture
def stub():
    return StubClassifier.from_file(config.STUB_SCRIPT)


@pytest.fixture
def scans_dir(tmp_path):
    return str(tmp_path / "scans")


def run(stub, scout_db, scans_dir, image, **kw):
    return scan_plant(stub, scout_db, image_bytes=image, now=NOW, scans_dir=scans_dir, **kw)


def test_scan_with_qr_saves_photo_and_row(stub, scout_db, scans_dir):
    image = photo("P-02")
    result = run(stub, scout_db, scans_dir, image)

    assert result["plant_id"] == "P-02" and result["qr_found"]
    assert result["plant"]["crop"] == "pepper"
    assert result["condition_id"] == "pepper_spoiled"
    assert result["status"] == "action"              # spoiled: severity high -> "Remove"
    assert result["condition"]["verified"] == 1      # sourced advice
    assert result["condition"]["what_to_do_now"].startswith("Remove from batch")
    assert [t["id"] for t in result["top3"]][0] == "pepper_spoiled"
    assert result["top3"][0]["name"] == "Spoiled green pepper"
    assert result["model_name"] == "stub (scripted demo)"
    assert result["image_path"] == "scans/20260927-143012_P-02.jpg"
    assert result["timestamp"] == "2026-09-27T14:30:12"

    with open(result["image_file"], "rb") as f:
        assert f.read() == image                     # saved exactly as sent, no re-encoding

    conn = db.open_db(scout_db)
    row = db.get_scan(conn, result["scan_id"])
    assert row["plant_id"] == "P-02" and row["status"] == "action"
    assert row["top1_condition_id"] == "pepper_spoiled" and row["confidence"] == 0.83
    assert row["top3"][0] == ["pepper_spoiled", 0.83]
    assert 130 < row["brightness"] < 180


@pytest.mark.parametrize("plant_id, status", [
    ("T-01", "healthy"), ("T-02", "action"), ("T-03", "uncertain"), ("T-04", "healthy"),
    ("P-01", "healthy"), ("P-02", "action"), ("P-03", "uncertain"), ("P-04", "action"),
])
def test_demo_script_statuses(stub, scout_db, scans_dir, plant_id, status):
    assert run(stub, scout_db, scans_dir, photo(plant_id))["status"] == status


def test_no_qr_is_unknown_and_uncertain(stub, scout_db, scans_dir):
    result = run(stub, scout_db, scans_dir, photo())
    assert result["plant_id"] == "unknown" and not result["qr_found"]
    assert result["plant"] is None
    assert result["status"] == "uncertain"
    assert "no QR" in result["notes"]


def test_plant_id_override_skips_qr(stub, scout_db, scans_dir):
    result = run(stub, scout_db, scans_dir, photo("P-01"), plant_id="T-02")
    assert result["plant_id"] == "T-02" and not result["qr_found"]


def test_qr_not_in_plants_file_is_added(stub, scout_db, scans_dir):
    result = run(stub, scout_db, scans_dir, photo("Q-07"))
    assert result["plant_id"] == "Q-07"
    assert "not in plants.json" in result["notes"]
    conn = db.open_db(scout_db)
    assert db.get_plant(conn, "Q-07")["label"] == "added by scan"


class FixedClassifier(Classifier):
    name = "fixed"

    def __init__(self, preds):
        self.preds = preds

    def predict(self, image, plant_id=None):
        return self.preds


def test_crop_filter_drops_other_crop_without_rescaling(scout_db, scans_dir):
    # A tomato batch: the model's pepper answer can't be right, and is dropped.
    clf = FixedClassifier([("pepper_spoiled", 0.7), ("tomato_spoiled", 0.2),
                           ("tomato_fresh", 0.1)])
    result = scan_plant(clf, scout_db, image_bytes=photo("T-01"), now=NOW, scans_dir=scans_dir)
    assert [t["id"] for t in result["top3"]] == ["tomato_spoiled", "tomato_fresh"]
    assert result["confidence"] == 0.2
    assert result["status"] == "uncertain"
    assert "pepper_spoiled" in result["notes"]


def test_stub_demo_shows_the_crop_filter(stub, scout_db, scans_dir):
    result = run(stub, scout_db, scans_dir, photo("T-02"))
    assert [t["id"] for t in result["top3"]] == ["tomato_spoiled", "tomato_fresh"]
    assert "ignored other-crop results: pepper_spoiled" in result["notes"]


def test_label_missing_from_knowledge_base_is_uncertain(scout_db, scans_dir):
    clf = FixedClassifier([("banana_wilt", 0.99)])
    result = scan_plant(clf, scout_db, image_bytes=photo(), now=NOW, scans_dir=scans_dir)
    assert result["status"] == "uncertain" and result["condition"] is None
    assert "banana_wilt is not in the knowledge base" in result["notes"]


def test_background_top1_is_saved_as_no_produce(scout_db, scans_dir):
    clf = FixedClassifier([("background", 0.92), ("tomato_fresh", 0.05), ("pepper_fresh", 0.03)])
    result = scan_plant(clf, scout_db, image_bytes=photo("T-01"), now=NOW, scans_dir=scans_dir)
    assert result["status"] == "no_produce"
    assert result["condition_id"] == "background" and result["condition"] is None
    assert result["top3"][0]["name"] == "No tomato or green pepper"
    assert "not in the knowledge base" not in (result["notes"] or "")
    row = db.get_scan(db.open_db(scout_db), result["scan_id"])
    assert row["status"] == "no_produce" and row["top1_condition_id"] == "background"


def test_background_below_threshold_is_still_no_produce(scout_db, scans_dir):
    clf = FixedClassifier([("background", 0.40), ("tomato_spoiled", 0.35), ("tomato_fresh", 0.25)])
    result = scan_plant(clf, scout_db, image_bytes=photo(), now=NOW, scans_dir=scans_dir)
    assert result["status"] == "no_produce"


def test_produce_below_threshold_is_uncertain_not_no_produce(scout_db, scans_dir):
    clf = FixedClassifier([("tomato_spoiled", 0.45), ("background", 0.40), ("tomato_fresh", 0.15)])
    result = scan_plant(clf, scout_db, image_bytes=photo("T-01"), now=NOW, scans_dir=scans_dir)
    assert result["status"] == "uncertain"


def test_crop_filter_never_drops_background(scout_db, scans_dir):
    # Tomato batch: pepper is dropped, background stays and becomes the top-1.
    clf = FixedClassifier([("pepper_fresh", 0.6), ("background", 0.3), ("tomato_fresh", 0.1)])
    result = scan_plant(clf, scout_db, image_bytes=photo("T-01"), now=NOW, scans_dir=scans_dir)
    assert [t["id"] for t in result["top3"]] == ["background", "tomato_fresh"]
    assert result["status"] == "no_produce"
    assert "ignored other-crop results: pepper_fresh" in result["notes"]
    conn = db.open_db(scout_db)
    for crop in ("tomato", "pepper"):
        kept, dropped = pipeline.crop_filter(conn, [("background", 0.5)], crop)
        assert kept == [("background", 0.5)] and dropped == []


def test_dark_photo_is_noted(stub, scout_db, scans_dir):
    result = run(stub, scout_db, scans_dir, photo(brightness=20))
    assert result["brightness"] < pipeline.DARK_BRIGHTNESS
    assert "dark" in result["notes"]


def test_two_scans_same_second_keep_both_photos(stub, scout_db, scans_dir):
    a = run(stub, scout_db, scans_dir, photo("T-01"))
    b = run(stub, scout_db, scans_dir, photo("T-01"))
    assert a["image_file"] != b["image_file"]
    assert os.path.exists(a["image_file"]) and os.path.exists(b["image_file"])


class FakeReply:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("HTTP %d" % self.status_code)


def test_snapshot_is_fetched_from_url(stub, scout_db, scans_dir, monkeypatch):
    asked = []
    monkeypatch.setattr(pipeline.requests, "get",
                        lambda url, timeout: asked.append((url, timeout)) or FakeReply(photo("T-01")))
    url = config.snapshot_url_for("192.168.50.1:5000")
    result = scan_plant(stub, scout_db, snapshot_url=url, now=NOW, scans_dir=scans_dir)
    assert asked == [("http://192.168.50.1:8080/?action=snapshot", config.SNAPSHOT_TIMEOUT_S)]
    assert result["plant_id"] == "T-01"


@pytest.mark.parametrize("fake_get", [
    lambda url, timeout: (_ for _ in ()).throw(requests.ConnectionError("refused")),
    lambda url, timeout: FakeReply(b"", 500),
    lambda url, timeout: FakeReply(b"<html>not a jpeg</html>"),
])
def test_snapshot_failure_saves_nothing(stub, scout_db, scans_dir, monkeypatch, fake_get):
    monkeypatch.setattr(pipeline.requests, "get", fake_get)
    with pytest.raises(ScanError):
        scan_plant(stub, scout_db, snapshot_url="http://x/", now=NOW, scans_dir=scans_dir)
    assert not os.path.exists(scans_dir) or not os.listdir(scans_dir)
    assert db.list_scans(db.open_db(scout_db)) == []


@pytest.mark.parametrize("address", ["192.168.50.1:5000", "http://192.168.50.1:5000/",
                                     "192.168.50.1", " 127.0.0.1:5000 "])
def test_snapshot_url_for(address):
    host = address.strip().replace("http://", "").split(":")[0].rstrip("/")
    assert config.snapshot_url_for(address) == "http://%s:8080/?action=snapshot" % host
