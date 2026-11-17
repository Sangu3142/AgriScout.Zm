"""Phase 5a: QR plant stakes (payload, decode from generated photos, printable sheets)."""

import json
import os
import re

import cv2
import numpy as np
import pytest

from scout import stakes
from scout.qr import decode_plant_id, make_payload, make_qr, parse_payload

FRAME_W, FRAME_H = 640, 480  # mjpg-streamer's snapshot size on the tank


def frame_with(qr: np.ndarray, x: int = 100, y: int = 80) -> np.ndarray:
    """A grey-ish 640x480 'camera photo' (BGR) with the QR pasted in."""
    frame = np.full((FRAME_H, FRAME_W), 140, np.uint8)
    frame[y:y + qr.shape[0], x:x + qr.shape[1]] = qr
    return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)


def rotated(img: np.ndarray, degrees: float) -> np.ndarray:
    """Rotate around the centre on a bigger white canvas, so no corner is cut off."""
    h, w = img.shape
    side = int((h ** 2 + w ** 2) ** 0.5) + 2
    canvas = np.full((side, side), 255, np.uint8)
    canvas[(side - h) // 2:(side - h) // 2 + h, (side - w) // 2:(side - w) // 2 + w] = img
    m = cv2.getRotationMatrix2D((side / 2, side / 2), degrees, 1.0)
    return cv2.warpAffine(canvas, m, (side, side), borderValue=255)


@pytest.mark.parametrize("text, expected", [
    ("AGRISCOUT:M-01", "M-01"),
    ("agriscout:t-03", "T-03"),
    ("AGRISCOUT:P-01", "P-01"),     # green pepper batches
    ("AGRISCOUT: M-02 ", "M-02"),
    ("AGRISCOUT:M-1", None),        # IDs have 2-3 digits
    ("AGRISCOUT:../x", None),
    ("https://example.com", None),
    ("", None),
    (None, None),
])
def test_parse_payload(text, expected):
    assert parse_payload(text) == expected


def test_decode_generated_qr():
    assert decode_plant_id(frame_with(make_qr(make_payload("P-01"), 200))) == "P-01"


@pytest.mark.parametrize("degrees", [30, 90, 180])
def test_decode_rotated_qr(degrees):
    qr = rotated(make_qr(make_payload("T-02"), 180), degrees)
    assert decode_plant_id(frame_with(qr, 60, 20)) == "T-02"


def test_decode_small_far_away_qr():
    """About 60 px for the whole code: a stake a few metres from the camera."""
    qr = make_qr(make_payload("P-03"), 60)
    assert qr.shape[0] < 100
    assert decode_plant_id(frame_with(qr, 400, 300)) == "P-03"


def test_no_qr_gives_none():
    assert decode_plant_id(np.full((FRAME_H, FRAME_W, 3), 120, np.uint8)) is None


def test_foreign_qr_is_ignored():
    assert decode_plant_id(frame_with(make_qr("hello", 200))) is None


def test_make_qr_code_is_at_least_the_asked_size():
    qr = make_qr(make_payload("M-01"), 709)
    assert set(np.unique(qr)) == {0, 255}  # pure black and white, no blurred grey
    assert stakes.code_width_px("M-01") >= stakes.cm_to_px(stakes.MIN_QR_CM) == 709


PLANTS = [{"id": "T-0%d" % i, "crop": "tomato", "row": "A", "label": "", "notes": ""}
          for i in range(1, 5)] + \
         [{"id": "P-0%d" % i, "crop": "pepper", "row": "B", "label": "", "notes": ""}
          for i in range(1, 5)]


@pytest.mark.parametrize("plant, expected", [
    ({"id": "T-01", "crop": "tomato", "row": "A", "label": "Tomato batch 1"},
     "Tomato batch - row A"),
    ({"id": "P-04", "crop": "pepper", "row": "B", "label": "spare"},
     "Green pepper batch - row B (spare)"),
    ({"id": "Q-07", "crop": None, "row": None, "label": ""}, "? batch - row ?"),
])
def test_stake_label_says_batch(plant, expected):
    assert stakes.label_details(plant) == expected


def test_sheets_are_a4_and_every_label_decodes():
    pages = stakes.build_sheets(PLANTS)
    assert len(pages) == 2
    cell_w, cell_h = stakes.PAGE_W // stakes.COLS, stakes.PAGE_H // stakes.ROWS
    found = []
    for page in pages:
        assert page.shape == (3508, 2480)
        for row in range(stakes.ROWS):
            for col in range(stakes.COLS):
                label = page[row * cell_h:(row + 1) * cell_h, col * cell_w:(col + 1) * cell_w]
                found.append(decode_plant_id(label))
    assert found == [p["id"] for p in PLANTS]


def test_pdf_structure(tmp_path):
    pages = [np.full((40, 30), 255, np.uint8), np.zeros((40, 30), np.uint8)]
    path = tmp_path / "t.pdf"
    stakes.write_pdf(pages, str(path))
    data = path.read_bytes()
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    assert len(re.findall(rb"/Type /Page\b", data)) == 2
    assert b"/Count 2" in data
    # startxref must point at the "xref" table, or readers call the file damaged.
    xref_at = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[xref_at:xref_at + 4] == b"xref"
    # every object offset in the table points at "<n> 0 obj"
    offsets = [int(m) for m in re.findall(rb"(\d{10}) 00000 n", data)]
    for number, offset in enumerate(offsets, start=1):
        assert data[offset:].startswith(b"%d 0 obj" % number)


def test_make_stakes_writes_png_pages_and_pdf(tmp_path):
    plants = tmp_path / "plants.json"
    plants.write_text(json.dumps({"plants": PLANTS[:5]}), encoding="utf-8")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "stakes_9.png").write_bytes(b"old")
    paths = stakes.make_stakes(str(plants), str(tmp_path / "out"))
    names = sorted(os.path.basename(p) for p in paths)
    assert names == ["stakes.pdf", "stakes_1.png", "stakes_2.png"]
    assert not (tmp_path / "out" / "stakes_9.png").exists()
