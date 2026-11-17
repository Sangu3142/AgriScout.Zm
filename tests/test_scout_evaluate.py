"""python -m scout.evaluate: accuracy per class, confusion matrix and wrong photos.

A fake classifier (decides by colour) on small solid-colour JPEGs: no ONNX model needed.
"""

import re

import cv2
import numpy as np
import pytest

from scout import config, evaluate

LABELS = ["background", "pepper_fresh", "tomato_fresh"]       # like models/labels.txt now
RED, GREEN, GREY = (0, 0, 255), (0, 255, 0), (128, 128, 128)  # BGR, OpenCV's order


class ColourClassifier:
    """Fake model: a red photo is tomato_fresh, a green one pepper_fresh, anything else
    background. It only works if it gets the photo in BGR, as decode_image() gives it."""
    labels = LABELS

    def __init__(self):
        self.shapes = []

    def predict(self, image, plant_id=None):
        self.shapes.append(image.shape)
        blue, green, red = image.reshape(-1, 3).mean(axis=0)
        if red > 200 and green < 50:
            top = "tomato_fresh"
        elif green > 200 and red < 50:
            top = "pepper_fresh"
        else:
            top = "background"
        others = [label for label in LABELS if label != top]
        return [(top, 0.8), (others[0], 0.15), (others[1], 0.05)]


def write_photo(path, bgr):
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, jpeg = cv2.imencode(".jpg", np.full((60, 80, 3), bgr, np.uint8))
    assert ok
    path.write_bytes(jpeg.tobytes())


@pytest.fixture
def testset(tmp_path):
    root = tmp_path / "testset"
    write_photo(root / "tomato_fresh" / "t1.jpg", RED)
    write_photo(root / "tomato_fresh" / "t2.JPG", RED)       # any case, like zip_dataset
    write_photo(root / "tomato_fresh" / "t3.jpg", GREEN)     # the model gets this one wrong
    write_photo(root / "pepper_fresh" / "p1.jpg", GREEN)
    write_photo(root / "pepper_fresh" / "p2.jpg", GREEN)
    write_photo(root / "background" / "b1.jpg", GREY)
    write_photo(root / "tomato_spoiled" / "s1.jpg", RED)     # not a class of this model
    (root / "pepper_fresh" / "broken.jpg").write_bytes(b"not a picture")
    (root / "pepper_fresh" / "notes.txt").write_text("not a .jpg: ignored")
    (root / "readme.txt").write_text("not a folder: ignored")
    return root


def test_counts_every_photo_in_a_confusion_matrix(testset):
    classifier = ColourClassifier()
    result = evaluate.evaluate_folder(classifier, str(testset))
    assert result["labels"] == LABELS
    assert result["matrix"] == {
        "background": {"background": 1, "pepper_fresh": 0, "tomato_fresh": 0},
        "pepper_fresh": {"background": 0, "pepper_fresh": 2, "tomato_fresh": 0},
        "tomato_fresh": {"background": 0, "pepper_fresh": 1, "tomato_fresh": 2},
    }
    assert classifier.shapes == [(60, 80, 3)] * 6            # decoded photos, full size
    [(path, true, predictions)] = result["wrong"]
    assert path == str(testset / "tomato_fresh" / "t3.jpg") and true == "tomato_fresh"
    assert predictions[0] == ("pepper_fresh", 0.8)
    assert result["no_photos"] == []


def test_skips_other_classes_and_broken_photos(testset):
    result = evaluate.evaluate_folder(ColourClassifier(), str(testset))
    assert result["skipped"] == [
        "skipped %s: not a picture" % (testset / "pepper_fresh" / "broken.jpg"),
        "skipped %s: not a class of this model" % (testset / "tomato_spoiled")]


def test_model_classes_without_photos_are_listed(tmp_path):
    write_photo(tmp_path / "testset" / "tomato_fresh" / "t1.jpg", RED)
    result = evaluate.evaluate_folder(ColourClassifier(), str(tmp_path / "testset"))
    assert result["no_photos"] == ["background", "pepper_fresh"]
    report = evaluate.format_report(result, "m.onnx", "testset")
    assert "no photos for: background, pepper_fresh" in report
    assert re.search(r"^  tomato_fresh\s+1/1\s+100\.0 %$", report, re.M)


def test_report(testset):
    result = evaluate.evaluate_folder(ColourClassifier(), str(testset))
    report = evaluate.format_report(result, "models/plant_model.onnx", str(testset))
    assert "model:   models/plant_model.onnx" in report
    assert "classes: background, pepper_fresh, tomato_fresh" in report
    assert "not a class of this model" in report and "not a picture" in report
    # accuracy per class, then overall
    assert re.search(r"^  background\s+1/1\s+100\.0 %$", report, re.M)
    assert re.search(r"^  pepper_fresh\s+2/2\s+100\.0 %$", report, re.M)
    assert re.search(r"^  tomato_fresh\s+2/3\s+66\.7 %$", report, re.M)
    assert re.search(r"^  overall\s+5/6\s+83\.3 %$", report, re.M)
    # confusion matrix: a header with every model class, one row per true class
    assert re.search(r"^  true \\ said\s+background  pepper_fresh  tomato_fresh$", report, re.M)
    assert re.search(r"^  tomato_fresh\s+0\s+1\s+2$", report, re.M)
    # every wrong photo with what the model said
    assert "Wrong (1)" in report
    assert ("%s  true: tomato_fresh  said: pepper_fresh 80 %%, background 15 %%, tomato_fresh 5 %%"
            % (testset / "tomato_fresh" / "t3.jpg")) in report


def test_report_without_photos(tmp_path):
    (tmp_path / "testset").mkdir()
    result = evaluate.evaluate_folder(ColourClassifier(), str(tmp_path / "testset"))
    assert "No photos to test." in evaluate.format_report(result, "m.onnx", "testset")


@pytest.fixture
def fake_model(monkeypatch):
    """main() loads ColourClassifier instead of an ONNX file; returns the paths it was asked for."""
    asked = []

    def load_model(path):
        asked.append(path)
        return ColourClassifier()
    monkeypatch.setattr(evaluate, "load_model", load_model)
    return asked


def test_main_prints_and_saves_the_same_report(testset, tmp_path, fake_model, capsys):
    saved = tmp_path / "result.txt"
    evaluate.main([str(testset), "--save", str(saved)])
    report = saved.read_text(encoding="utf-8")
    assert capsys.readouterr().out == report + "saved %s\n" % saved
    assert "overall" in report and "Wrong (1)" in report
    assert fake_model == [config.MODEL_PATH]                  # default: models/plant_model.onnx


def test_main_with_another_model(testset, fake_model, capsys):
    evaluate.main([str(testset), "--model", r"models\trial\plant_model.onnx"])
    assert fake_model == [r"models\trial\plant_model.onnx"]
    assert "model:   models\\trial\\plant_model.onnx" in capsys.readouterr().out


def test_main_missing_folder(tmp_path):
    with pytest.raises(SystemExit, match="no such folder"):
        evaluate.main([str(tmp_path / "nope")])


def test_load_model_needs_labels_next_to_the_model(tmp_path):
    with pytest.raises(SystemExit, match="plant_model.onnx"):
        evaluate.load_model(str(tmp_path / "plant_model.onnx"))
    (tmp_path / "plant_model.onnx").write_bytes(b"model")
    with pytest.raises(SystemExit, match="labels.txt"):
        evaluate.load_model(str(tmp_path / "plant_model.onnx"))
