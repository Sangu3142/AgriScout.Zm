"""Pack dataset/ into one zip for Colab: dataset/<class>/*.jpg + labels.txt.

Usage (repo root):
  python -m scout.zip_dataset                    -> dataset.zip
  python -m scout.zip_dataset --out C:\\temp\\d.zip

labels.txt lists the classes in alphabetical order, the order Colab's ImageFolder gives them
(background, pepper_fresh, ...). Use the labels.txt Colab writes next to the exported model as
models/labels.txt: OnnxClassifier reads the output order only from that file.
The zip is not compressed (ZIP_STORED): JPEGs are already compressed, so it would only be slower.
"""

import argparse
import os
import zipfile
from typing import Dict

from scout import config
from scout.classifier import MODEL_CLASSES

MIN_PER_CLASS = 50  # fewer photos than this per class: training will be weak, say so


def zip_dataset(dataset_dir: str = config.DATASET_DIR,
                out_path: str = os.path.join(config.REPO_DIR, "dataset.zip")) -> Dict[str, int]:
    """Write the zip. Returns {class: number of photos}, every model class included (0 if none).

    Only the model-class folders and .jpg files go in: a stray folder or file is left out.
    """
    counts = {}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("labels.txt", "\n".join(sorted(MODEL_CLASSES)) + "\n")  # = ImageFolder order
        for cls in MODEL_CLASSES:
            folder = os.path.join(dataset_dir, cls)
            names = sorted(n for n in os.listdir(folder)
                           if n.lower().endswith(".jpg")) if os.path.isdir(folder) else []
            for name in names:
                z.write(os.path.join(folder, name), "dataset/%s/%s" % (cls, name))
            counts[cls] = len(names)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Zip dataset/ for training in Colab")
    parser.add_argument("--dataset", default=config.DATASET_DIR, help="dataset folder")
    parser.add_argument("--out", default=os.path.join(config.REPO_DIR, "dataset.zip"))
    args = parser.parse_args()
    counts = zip_dataset(args.dataset, args.out)
    for cls, n in counts.items():
        warning = "   <- fewer than %d, take more" % MIN_PER_CLASS if n < MIN_PER_CLASS else ""
        print("%-16s %4d%s" % (cls, n, warning))
    print("wrote %s (%.1f MB)" % (args.out, os.path.getsize(args.out) / 1e6))


if __name__ == "__main__":
    main()
