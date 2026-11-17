"""Plant condition classifiers + the rule that turns a diagnosis into a status.

Every classifier has the same interface, so the pipeline doesn't care which one runs:
    predict(image, plant_id=None) -> [(condition_id, confidence), ...]   top 3, best first

- StubClassifier: scripted answers per plant (data/stub_script.json), ignores the photo.
  Its name "stub (scripted demo)" is saved with every scan and shown on screen, so nobody
  mistakes it for a real model.
- OnnxClassifier: a trained model in models/plant_model.onnx + models/labels.txt, run with
  OpenCV's dnn module (already installed, no onnxruntime needed).
load_classifier() picks the ONNX model when both files exist, else the stub.
"""

import json
import os
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from scout import config

Prediction = Tuple[str, float]  # (condition_id, confidence 0..1)

# The classes a model may predict: fresh or spoiled produce for each crop, plus "background"
# (no tomato or green pepper in the photo: empty tray, backdrop, hands, the QR label alone,
# other fruit such as apples and oranges, other red/green objects).
# The model is trained on our own photos (python -m scout.capture).
# This list is only the set of allowed classes and the capture/zip folders. The model's output
# order comes ONLY from models/labels.txt (Colab's ImageFolder sorts classes alphabetically).
BACKGROUND = "background"
BACKGROUND_NAME = "No tomato or green pepper"  # how it is shown in a top-3 list
MODEL_CLASSES = ["tomato_fresh", "tomato_spoiled", "pepper_fresh", "pepper_spoiled", BACKGROUND]
TOP_K = 3
STUB_NAME = "stub (scripted demo)"


def is_stub(model_name: str) -> bool:
    """True for scans made by StubClassifier (it ignores the photo)."""
    return model_name.startswith("stub")


class Classifier:
    name = "base"

    def predict(self, image: np.ndarray, plant_id: Optional[str] = None) -> List[Prediction]:
        """Top-3 (condition_id, confidence), best first. image is BGR (OpenCV order).

        plant_id is only used by StubClassifier (to script a demo); real models ignore it.
        """
        raise NotImplementedError


# ---------------------------------------------------------------- stub

DEFAULT_STUB_SCRIPT = {
    "default": [["tomato_fresh", 0.45], ["pepper_fresh", 0.30], ["tomato_spoiled", 0.10]],
    "plants": {},
}


class StubClassifier(Classifier):
    name = STUB_NAME

    def __init__(self, script: Optional[dict] = None):
        script = script or DEFAULT_STUB_SCRIPT
        self.default = self._check(script["default"], "default")
        self.plants: Dict[str, List[Prediction]] = {
            plant_id: self._check(preds, plant_id)
            for plant_id, preds in script.get("plants", {}).items()}

    @staticmethod
    def _check(preds: list, where: str) -> List[Prediction]:
        preds = [(str(cid), float(conf)) for cid, conf in preds]
        for cid, conf in preds:
            if cid not in MODEL_CLASSES:
                raise ValueError("stub script %s: %r is not a model class" % (where, cid))
            if not 0.0 <= conf <= 1.0:
                raise ValueError("stub script %s: confidence %r not in 0..1" % (where, conf))
        return sorted(preds, key=lambda p: p[1], reverse=True)[:TOP_K]

    @classmethod
    def from_file(cls, path: str = config.STUB_SCRIPT) -> "StubClassifier":
        if not os.path.exists(path):
            return cls()
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def predict(self, image: np.ndarray, plant_id: Optional[str] = None) -> List[Prediction]:
        return list(self.plants.get(plant_id, self.default))


# ---------------------------------------------------------------- ONNX model

INPUT_SIZE = 224                           # model input: 224 x 224 RGB
MEAN = np.array([0.485, 0.456, 0.406])     # ImageNet normalisation (most pretrained CNNs)
STD = np.array([0.229, 0.224, 0.225])


def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max())  # minus max: avoids overflow, same result
    return e / e.sum()


class OnnxClassifier(Classifier):
    def __init__(self, model_path: str = config.MODEL_PATH,
                 labels_path: str = config.LABELS_PATH,
                 known_ids: Optional[set] = None):
        """known_ids: condition ids in the knowledge base. A label outside it (other than
        "background", which has no advice) is an error now, at load time, rather than a scan
        that can't show any advice later.

        self.labels[i] is the class of output i: read from labels.txt, never from MODEL_CLASSES.
        """
        with open(labels_path, encoding="utf-8") as f:
            self.labels = [line.strip() for line in f if line.strip()]
        if known_ids is not None:
            allowed = set(known_ids) | {BACKGROUND}
            unknown = [label for label in self.labels if label not in allowed]
            if unknown:
                raise ValueError("%s: labels not in the knowledge base: %s"
                                 % (labels_path, ", ".join(unknown)))
        self.net = cv2.dnn.readNetFromONNX(model_path)
        self.name = "onnx:" + os.path.basename(model_path)

    def preprocess(self, image: np.ndarray) -> np.ndarray:
        """BGR photo -> 1 x 3 x 224 x 224 float blob, RGB, normalised like in training."""
        blob = cv2.dnn.blobFromImage(image, scalefactor=1 / 255.0,
                                     size=(INPUT_SIZE, INPUT_SIZE), swapRB=True)
        return ((blob - MEAN.reshape(1, 3, 1, 1)) / STD.reshape(1, 3, 1, 1)).astype(np.float32)

    def predict(self, image: np.ndarray, plant_id: Optional[str] = None) -> List[Prediction]:
        self.net.setInput(self.preprocess(image))
        scores = self.net.forward().reshape(-1).astype(np.float64)
        if len(scores) != len(self.labels):
            raise ValueError("model gives %d scores but labels.txt has %d labels"
                             % (len(scores), len(self.labels)))
        # Some models end with softmax (already probabilities), others give raw scores.
        if scores.min() < 0 or abs(scores.sum() - 1.0) > 1e-3:
            scores = softmax(scores)
        best = np.argsort(scores)[::-1][:TOP_K]
        return [(self.labels[i], float(scores[i])) for i in best]


def load_classifier(model_path: str = config.MODEL_PATH, labels_path: str = config.LABELS_PATH,
                    stub_script: str = config.STUB_SCRIPT,
                    known_ids: Optional[set] = None) -> Classifier:
    if os.path.exists(model_path) and os.path.exists(labels_path):
        return OnnxClassifier(model_path, labels_path, known_ids)
    return StubClassifier.from_file(stub_script)


# ---------------------------------------------------------------- status rule

def status_for(condition: Optional[dict], confidence: Optional[float],
               threshold: float = config.CONF_THRESHOLD) -> str:
    """healthy / watch / action / uncertain.

    uncertain: no diagnosis we can look up, or the model isn't sure enough (below threshold).
    Otherwise healthy for a healthy class; else severity low = watch, medium/high = action.
    """
    if condition is None or confidence is None or confidence < threshold:
        return "uncertain"
    if condition["type"] == "healthy":
        return "healthy"
    return "watch" if condition["severity"] == "low" else "action"


def scan_status(condition_id: Optional[str], condition: Optional[dict],
                confidence: Optional[float], threshold: float = config.CONF_THRESHOLD) -> str:
    """Status of a scan from its top-1 result (after the crop filter).

    no_produce: the top-1 class is "background", whatever its score (nothing to diagnose).
    Otherwise status_for(): a produce class below the threshold is still "uncertain".
    """
    if condition_id == BACKGROUND:
        return "no_produce"
    return status_for(condition, confidence, threshold)
