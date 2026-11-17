# models/

The trained AgriScout model used by the app.

- `plant_model.onnx`: MobileNetV2 image classifier (v6). Input 1 x 3 x 224 x 224 RGB with ImageNet
  mean/std normalisation (constants at the top of `scout/classifier.py`); output one score per class.
  Run offline with OpenCV's `cv2.dnn`.
- `labels.txt`: one class per line, in the model's output order. Every id must exist in
  `data/conditions_seed.json`, or loading stops with an error.

Classes: background (no produce), pepper_fresh, pepper_spoiled, tomato_fresh, tomato_spoiled.

Accuracy: 98.9% on a held-out test set of 522 photos (see `docs/RESULTS.md`).

Training photos are our own:
`python -m scout.capture --class tomato_fresh --out dataset` saves photos to `dataset/<class>/`, and
`python -m scout.zip_dataset` packs `dataset/` and a matching `labels.txt` for training in Google Colab.

When both files exist, the GUI, `python -m scout.server` and `python -m scout.pipeline` use the model;
otherwise they fall back to a scripted stub.
