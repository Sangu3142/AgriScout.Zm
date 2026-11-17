# AgriScout.Zm model results

## Model

- **Architecture:** MobileNetV2, fine-tuned with transfer learning in PyTorch (Google Colab, T4 GPU)
- **Deployment:** exported to ONNX and run offline on the laptop
- **Input:** one photo from the robot's camera
- **Output:** one of five classes, with a confidence score. Below 60% confidence the app shows "Not sure" instead of a label.

## Classes and training data

| Class | Training photos |
|---|---|
| background (no produce) | 2,835 |
| pepper_fresh | 2,586 |
| pepper_spoiled | 633 |
| tomato_fresh | 1,771 |
| tomato_spoiled | 1,333 |
| **Total** | **9,158** |

All photos were taken by the team with the robot's own camera, against our demo backdrop and lamp.

## Test results

The final model was evaluated on a **held-out test set of 522 photos** that were never used in training.

| Metric | Result |
|---|---|
| Overall accuracy | **98.9%** |

## How we improved the model

We trained and compared six model versions. After each version we read the confusion matrix
(which classes get mixed up), took targeted new photos of the weak classes, and retrained.
For example, a shrivelled tomato was being mistaken for a spoiled pepper, so we added
~350 more spoiled-tomato photos covering shrivelled, soft and mouldy fruit from all sides.

## Limitations

- Tested indoors only, with a fixed backdrop and lamp. Accuracy on real farms, in sunlight
  and with cluttered backgrounds, is not yet measured.
- Only tomatoes and peppers so far.
- Some background photos were occasionally mistaken for fresh tomatoes in training
  validation; this is a known area to improve with more varied background photos.

## Reproduce

```powershell
.venv\Scripts\python -m scout.evaluate --model models\plant_model.onnx --save eval.txt testset
```
