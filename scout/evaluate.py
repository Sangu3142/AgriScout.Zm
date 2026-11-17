"""Test a trained model on photos it has never seen: testset/<class>/*.jpg.

Usage (repo root):
  python -m scout.evaluate testset
  python -m scout.evaluate testset --model models\\trial\\plant_model.onnx
  python -m scout.evaluate testset --save results.txt

labels.txt is read from the model's folder. Each photo goes the same way as a scan's photo:
file bytes -> decode_image() -> classifier.predict() (scout/pipeline.py, scout/classifier.py).
Only the model is measured: its top-1 class. No QR code, no crop filter, no 60 % threshold.
The folder name is the true class. A folder that is not a class of this model is skipped.

testset/ must hold NEW photos, never ones used for training (dataset/): on photos it has already
seen, a model scores better than it really is. testset/ is git-ignored, like dataset/.
"""

import argparse
import os
from typing import List, Optional

from scout import config
from scout.classifier import OnnxClassifier
from scout.pipeline import ScanError, decode_image


def load_model(model_path: str = config.MODEL_PATH) -> OnnxClassifier:
    """The ONNX model and the labels.txt next to it."""
    labels_path = os.path.join(os.path.dirname(os.path.abspath(model_path)), "labels.txt")
    for path in (model_path, labels_path):
        if not os.path.isfile(path):
            raise SystemExit("not found: %s" % path)
    return OnnxClassifier(model_path, labels_path)


def evaluate_folder(classifier, testset_dir: str) -> dict:
    """Run the classifier on testset_dir/<class>/*.jpg.

    classifier: anything with .labels (its classes) and .predict(image) -> [(label, confidence),
    ...] best first: an OnnxClassifier, or a fake in the tests.
    Returns labels, matrix[true class][class said] = number of photos (only classes with photos),
    wrong = [(path, true class, predictions)], skipped = messages, no_photos = classes without any.
    """
    labels = list(classifier.labels)
    matrix, wrong, skipped = {}, [], []
    for name in sorted(os.listdir(testset_dir)):
        folder = os.path.join(testset_dir, name)
        if not os.path.isdir(folder):
            continue
        if name not in labels:
            skipped.append("skipped %s: not a class of this model" % folder)
            continue
        for file_name in sorted(n for n in os.listdir(folder) if n.lower().endswith(".jpg")):
            path = os.path.join(folder, file_name)
            with open(path, "rb") as f:
                image_bytes = f.read()
            try:
                image = decode_image(image_bytes)
            except ScanError:
                skipped.append("skipped %s: not a picture" % path)
                continue
            predictions = classifier.predict(image)
            said = predictions[0][0]
            matrix.setdefault(name, dict.fromkeys(labels, 0))[said] += 1
            if said != name:
                wrong.append((path, name, predictions))
    return {"labels": labels, "matrix": matrix, "wrong": wrong, "skipped": skipped,
            "no_photos": [label for label in labels if label not in matrix]}


def _accuracy_line(width: int, name: str, right: int, total: int) -> str:
    return "  %-*s %4d/%-4d %5.1f %%" % (width, name, right, total, 100.0 * right / total)


def format_report(result: dict, model_path: str, testset_dir: str) -> str:
    """The report as text: accuracy per class, confusion matrix, every wrong photo."""
    labels, matrix = result["labels"], result["matrix"]
    lines = ["AgriScout.Zm model test",
             "model:   %s" % model_path,
             "classes: %s" % ", ".join(labels),
             "photos:  %s" % testset_dir,
             ""]
    lines += result["skipped"]
    if result["no_photos"]:
        lines.append("no photos for: %s" % ", ".join(result["no_photos"]))
    if result["skipped"] or result["no_photos"]:
        lines.append("")
    if not matrix:
        lines.append("No photos to test.")
        return "\n".join(lines) + "\n"
    tested = [label for label in labels if label in matrix]  # rows, in the model's order
    head = "true \\ said"
    width = max(len(name) for name in tested + ["overall", head])

    lines.append("Accuracy (top-1)")
    right_sum = total_sum = 0
    for label in tested:
        right, total = matrix[label][label], sum(matrix[label].values())
        lines.append(_accuracy_line(width, label, right, total))
        right_sum, total_sum = right_sum + right, total_sum + total
    lines.append(_accuracy_line(width, "overall", right_sum, total_sum))
    lines.append("")

    lines.append("Confusion matrix (rows = true class, columns = what the model said)")
    lines.append("  %-*s  %s" % (width, head, "  ".join(labels)))
    for label in tested:
        counts = "  ".join("%*d" % (len(said), matrix[label][said]) for said in labels)
        lines.append("  %-*s  %s" % (width, label, counts))
    lines.append("")

    lines.append("Wrong (%d)" % len(result["wrong"]))
    for path, true, predictions in result["wrong"]:
        said = ", ".join("%s %.0f %%" % (label, conf * 100) for label, conf in predictions)
        lines.append("  %s  true: %s  said: %s" % (path, true, said))
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Test a model on photos it was not trained on")
    parser.add_argument("testset", help="folder with one sub-folder per class, e.g. testset")
    parser.add_argument("--model", default=config.MODEL_PATH,
                        help="ONNX model, labels.txt is read from its folder "
                             "(default: models/plant_model.onnx)")
    parser.add_argument("--save", help="also write the report to this text file (UTF-8)")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.testset):
        raise SystemExit("no such folder: %s" % args.testset)
    classifier = load_model(args.model)
    report = format_report(evaluate_folder(classifier, args.testset), args.model, args.testset)
    print(report, end="")
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(report)
        print("saved %s" % args.save)


if __name__ == "__main__":
    main()
