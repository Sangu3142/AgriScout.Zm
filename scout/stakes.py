"""Printable QR stake labels: A4 sheets (PNG, 300 dpi) + one PDF that prints them at exact size.

Usage (repo root):
  python -m scout.stakes                          every batch in data/plants.json
  python -m scout.stakes --out some\\folder

Print the PDF at 100 % / "actual size" (not "fit to page"): then each QR code is QR_CM wide.
"""

import argparse
import glob
import os
import zlib
from typing import List

import cv2
import numpy as np

from scout import config, db
from scout.qr import QUIET_ZONE_MODULES, make_payload, make_qr, qr_modules

DPI = 300
A4_MM = (210, 297)
PAGE_W = round(A4_MM[0] / 25.4 * DPI)   # 2480 px
PAGE_H = round(A4_MM[1] / 25.4 * DPI)   # 3508 px
COLS, ROWS = 2, 2                       # 4 big labels per page
QR_CM = 7.0                             # width of the code itself (black squares) on paper
MIN_QR_CM = 6.0                         # smaller codes are hard to read from the tank's camera
PDF_PAGE_PT = (595.28, 841.89)          # A4 in PDF points (1 pt = 1/72 inch)

FONT = cv2.FONT_HERSHEY_DUPLEX
BORDER_GRAY = 170                       # cut line: light grey, clearly not part of the code


def cm_to_px(cm: float) -> int:
    return round(cm / 2.54 * DPI)


def _put_centred(img: np.ndarray, text: str, centre_x: int, top: int, scale: float,
                 thickness: int) -> int:
    """Draw text centred on centre_x with its top at `top`. Returns the y below the text."""
    (w, h), baseline = cv2.getTextSize(text, FONT, scale, thickness)
    cv2.putText(img, text, (centre_x - w // 2, top + h), FONT, scale, 0, thickness, cv2.LINE_AA)
    return top + h + baseline


def label_details(plant: dict) -> str:
    """Text under the ID: 'Green pepper batch - row B', plus ' (spare)' for a spare stake.
    Hershey fonts are ASCII only, which is fine: crop names are plain English."""
    crop = db.CROP_NAMES.get(plant.get("crop"), plant.get("crop") or "?")
    details = "%s batch - row %s" % (crop, plant.get("row") or "?")
    if plant.get("label") == "spare":
        details += " (spare)"
    return details


def draw_label(plant: dict, width: int, height: int) -> np.ndarray:
    """One stake label: cut line, big QR, batch ID, "Tomato batch - row A"."""
    assert QR_CM >= MIN_QR_CM
    label = np.full((height, width), 255, np.uint8)
    cv2.rectangle(label, (20, 20), (width - 21, height - 21), BORDER_GRAY, 3)

    qr = make_qr(make_payload(plant["id"]), cm_to_px(QR_CM))
    top = 60
    x = (width - qr.shape[1]) // 2
    label[top:top + qr.shape[0], x:x + qr.shape[1]] = qr

    y = top + qr.shape[0] + 10
    y = _put_centred(label, plant["id"], width // 2, y, 6.0, 14)
    _put_centred(label, label_details(plant), width // 2, y + 40, 2.6, 5)
    return label


def build_sheets(plants: List[dict]) -> List[np.ndarray]:
    """A4 pages (grayscale, 300 dpi), COLS x ROWS labels each, in plant order."""
    cell_w, cell_h = PAGE_W // COLS, PAGE_H // ROWS
    per_page = COLS * ROWS
    pages = []
    for start in range(0, len(plants), per_page):
        page = np.full((PAGE_H, PAGE_W), 255, np.uint8)
        for i, plant in enumerate(plants[start:start + per_page]):
            row, col = divmod(i, COLS)
            page[row * cell_h:(row + 1) * cell_h,
                 col * cell_w:(col + 1) * cell_w] = draw_label(plant, cell_w, cell_h)
        pages.append(page)
    return pages


def code_width_px(plant_id: str) -> int:
    """Width of the printed code (black squares only) for this ID, in pixels at DPI."""
    qr = make_qr(make_payload(plant_id), cm_to_px(QR_CM))
    n = qr_modules(make_payload(plant_id)).shape[0]
    return qr.shape[0] * n // (n + 2 * QUIET_ZONE_MODULES)


# ---------------------------------------------------------------- PDF (stdlib only)

def write_pdf(pages: List[np.ndarray], path: str) -> None:
    """Write grayscale page images as an A4 PDF, one image per page, no library needed.

    A PDF is a list of numbered objects plus a table (xref) of where each object starts:
      1 = catalog, 2 = page list, then for each page: page, drawing commands, image.
    The image is stored zlib-compressed (/FlateDecode, which PDF readers decode themselves).
    """
    objects = []  # objects[i] is object number i + 1

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # page list: filled in below, once the page numbers are known
    w_pt, h_pt = PDF_PAGE_PT
    page_numbers = []
    for page in pages:
        height, width = page.shape
        data = zlib.compress(page.tobytes(), 9)
        image = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                    b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
                    b"/Length %d >>\nstream\n" % (width, height, len(data))
                    + data + b"\nendstream")
        # Drawing command: scale the 1x1 image to the whole page, then draw it.
        draw = b"q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (w_pt, h_pt)
        content = add(b"<< /Length %d >>\nstream\n" % len(draw) + draw + b"\nendstream")
        page_numbers.append(add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
            b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
            % (w_pt, h_pt, image, content)))
    kids = b" ".join(b"%d 0 R" % n for n in page_numbers)
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_numbers))

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref_at)
    with open(path, "wb") as f:
        f.write(out)


def make_stakes(plants_path: str = config.PLANTS_FILE, out_dir: str = config.STAKES_DIR) -> list:
    """Write stakes_1.png, stakes_2.png, ... and stakes.pdf. Returns the written paths."""
    plants = db.read_seed(plants_path, "plants", db.PLANT_KEYS)
    pages = build_sheets(plants)
    os.makedirs(out_dir, exist_ok=True)
    for old in glob.glob(os.path.join(out_dir, "stakes_*.png")):
        os.remove(old)  # a shorter plant list must not leave an old extra page behind
    paths = []
    for i, page in enumerate(pages, start=1):
        path = os.path.join(out_dir, "stakes_%d.png" % i)
        cv2.imwrite(path, page)
        paths.append(path)
    pdf_path = os.path.join(out_dir, "stakes.pdf")
    write_pdf(pages, pdf_path)
    paths.append(pdf_path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Make printable QR stake labels (A4)")
    parser.add_argument("--plants", default=config.PLANTS_FILE, help="plants JSON file")
    parser.add_argument("--out", default=config.STAKES_DIR, help="output folder")
    args = parser.parse_args()
    for path in make_stakes(args.plants, args.out):
        print("wrote", path)
    print("Print the PDF at 100 %% (actual size): each QR code is %.1f cm wide." % QR_CM)


if __name__ == "__main__":
    main()
