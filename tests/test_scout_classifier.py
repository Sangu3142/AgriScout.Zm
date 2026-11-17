"""Phase 5a/5c: classifier interface, stub, model loading and the status rule."""

import os

import numpy as np
import pytest

from scout import config, db
from scout.classifier import (BACKGROUND, MODEL_CLASSES, STUB_NAME, OnnxClassifier,
                              StubClassifier, is_stub, load_classifier, scan_status, softmax,
                              status_for)

IMAGE = np.zeros((480, 640, 3), np.uint8)
ALPHABETICAL = ["background", "pepper_fresh", "pepper_spoiled", "tomato_fresh", "tomato_spoiled"]


def test_model_classes_are_four_produce_classes_plus_background(scout_db):
    assert MODEL_CLASSES == ["tomato_fresh", "tomato_spoiled", "pepper_fresh", "pepper_spoiled",
                             "background"]
    conn = db.open_db(scout_db)
    produce = set(MODEL_CLASSES) - {BACKGROUND}
    assert produce == db.condition_ids(conn)  # no advice without a class, and back
    assert db.get_condition(conn, BACKGROUND) is None  # background has no advice on purpose


def test_stub_accepts_background():
    stub = StubClassifier({"default": [["background", 0.8], ["tomato_fresh", 0.1]]})
    assert stub.predict(IMAGE)[0] == ("background", 0.8)


class FakeNet:
    """Stands in for cv2.dnn's network: returns fixed raw scores."""

    def __init__(self, scores):
        self.scores = np.array([scores], np.float32)

    def setInput(self, blob):  # noqa: N802 (OpenCV's name)
        assert blob.shape == (1, 3, 224, 224)

    def forward(self):
        return self.scores


def fake_model(tmp_path, monkeypatch, labels, scores):
    path = tmp_path / "labels.txt"
    path.write_text("\n".join(labels) + "\n", encoding="utf-8")
    monkeypatch.setattr("scout.classifier.cv2.dnn.readNetFromONNX", lambda p: FakeNet(scores))
    return OnnxClassifier(str(tmp_path / "m.onnx"), str(path),
                          known_ids=set(MODEL_CLASSES) - {BACKGROUND})


def test_onnx_output_order_comes_from_labels_txt_only(tmp_path, monkeypatch):
    # Colab's ImageFolder sorts classes alphabetically, which is NOT the MODEL_CLASSES order.
    # Output 3 is the highest: labels.txt says that is tomato_fresh (MODEL_CLASSES[3] would say
    # pepper_spoiled).
    clf = fake_model(tmp_path, monkeypatch, ALPHABETICAL, [0.05, 0.1, 0.05, 0.7, 0.1])
    assert clf.labels == ALPHABETICAL
    preds = clf.predict(IMAGE)
    assert preds[0] == ("tomato_fresh", pytest.approx(0.7))
    assert {cid for cid, _ in preds[1:]} == {"pepper_fresh", "tomato_spoiled"}  # 0.1 each
    # output 0 = background in this file
    clf = fake_model(tmp_path, monkeypatch, ALPHABETICAL, [0.9, 0.04, 0.03, 0.02, 0.01])
    assert clf.predict(IMAGE)[0] == ("background", pytest.approx(0.9))


def test_onnx_accepts_background_but_not_unknown_labels(tmp_path, monkeypatch):
    fake_model(tmp_path, monkeypatch, ALPHABETICAL, [0.2] * 5)          # no error
    with pytest.raises(ValueError, match="banana_wilt"):
        fake_model(tmp_path, monkeypatch, ALPHABETICAL + ["banana_wilt"], [0.2] * 6)


def test_is_stub():
    assert is_stub(STUB_NAME) and is_stub(StubClassifier().name)
    assert not is_stub("onnx:plant_model.onnx")


def test_stub_script_file_only_uses_model_classes():
    stub = StubClassifier.from_file(config.STUB_SCRIPT)  # raises if a class is not allowed
    for preds in list(stub.plants.values()) + [stub.default]:
        assert {cid for cid, _ in preds} <= set(MODEL_CLASSES)


def test_stub_refuses_a_non_model_class():
    with pytest.raises(ValueError, match="maize_common_rust"):
        StubClassifier({"default": [["maize_common_rust", 0.9]]})


def test_stub_is_deterministic_sorted_top3():
    stub = StubClassifier.from_file(config.STUB_SCRIPT)
    first = stub.predict(IMAGE, "T-02")
    assert first == stub.predict(np.ones_like(IMAGE), "T-02")  # the photo is ignored
    assert len(first) == 3
    assert [c for _, c in first] == sorted([c for _, c in first], reverse=True)
    assert first[0][0] == "tomato_spoiled"


def test_stub_unknown_plant_gets_default():
    stub = StubClassifier.from_file(config.STUB_SCRIPT)
    assert stub.predict(IMAGE, "unknown") == stub.default
    assert stub.predict(IMAGE, None) == stub.default
    assert stub.default[0][1] < config.CONF_THRESHOLD  # no QR: shown as uncertain


def test_stub_name_says_it_is_not_a_model():
    assert "stub" in StubClassifier().name


def test_load_classifier_falls_back_to_stub(tmp_path):
    clf = load_classifier(str(tmp_path / "none.onnx"), str(tmp_path / "labels.txt"),
                          str(tmp_path / "no_script.json"))
    assert isinstance(clf, StubClassifier)


def test_onnx_refuses_labels_outside_knowledge_base(tmp_path):
    labels = tmp_path / "labels.txt"
    labels.write_text("tomato_fresh\nbanana_wilt\n", encoding="utf-8")
    with pytest.raises(ValueError, match="banana_wilt"):
        OnnxClassifier(str(tmp_path / "m.onnx"), str(labels), known_ids={"tomato_fresh"})


@pytest.mark.skipif(not os.path.exists(config.MODEL_PATH),
                    reason="no trained model in models/ yet (Phase 5b)")
def test_real_model_predicts_top3(scout_db):
    conn = db.open_db(scout_db)
    preds = OnnxClassifier(known_ids=db.condition_ids(conn)).predict(IMAGE)
    assert len(preds) == 3
    assert all(0 <= conf <= 1 for _, conf in preds)


def test_softmax_sums_to_one():
    p = softmax(np.array([2.0, 1.0, -1.0]))
    assert abs(p.sum() - 1) < 1e-9 and p[0] > p[1] > p[2]


# ---------------------------------------------------------------- status rule

HEALTHY = {"type": "healthy", "severity": "low"}      # fresh produce
RUST = {"type": "disease", "severity": "low"}         # no seed entry uses low/medium now,
EARLY = {"type": "disease", "severity": "medium"}     # but the rule still covers them
LATE = {"type": "disease", "severity": "high"}        # spoiled produce


@pytest.mark.parametrize("condition, conf, expected", [
    (HEALTHY, 0.95, "healthy"),
    (RUST, 0.87, "watch"),
    (EARLY, 0.82, "action"),
    (LATE, 0.74, "action"),
    (LATE, 0.60, "action"),       # exactly the threshold counts as sure enough
    (LATE, 0.59, "uncertain"),
    (HEALTHY, 0.59, "uncertain"),
    (None, 0.99, "uncertain"),    # label not in the knowledge base
    (RUST, None, "uncertain"),    # no prediction at all
])
def test_status_for(condition, conf, expected):
    assert status_for(condition, conf) == expected


@pytest.mark.parametrize("cid, condition, conf, expected", [
    ("background", None, 0.95, "no_produce"),
    ("background", None, 0.30, "no_produce"),   # background top-1 at any score
    ("tomato_spoiled", LATE, 0.45, "uncertain"),  # produce below 0.6 stays "Not sure"
    ("tomato_spoiled", LATE, 0.80, "action"),
    ("tomato_fresh", HEALTHY, 0.90, "healthy"),
    (None, None, None, "uncertain"),            # nothing left after the crop filter
])
def test_scan_status(cid, condition, conf, expected):
    assert scan_status(cid, condition, conf) == expected


def test_threshold_is_0_6():
    assert config.CONF_THRESHOLD == 0.6
