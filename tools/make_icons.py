"""Draw the AgriScout icon once (Windows, PySide6) and save it in every size we need.

  robot/web/icon-192.png, robot/web/icon-512.png   phone "Add to Home screen" (manifest.json)
  desktop/agriscout.ico                             Windows shortcut + GUI window icon

The PNGs/ICO are committed, so the Pi never needs an image library.
Run from the repo root:  .venv\\Scripts\\python tools\\make_icons.py
"""

import os
import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPen

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKGROUND = QColor("#2e7d32")  # same green as theme_color in manifest.json
LEAF = QColor("#c5e1a5")


def draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 100.0  # draw on a 100 x 100 grid, scaled

    # Rounded square background (full bleed, so Android's round mask still looks fine).
    p.setPen(Qt.NoPen)
    p.setBrush(BACKGROUND)
    p.drawRoundedRect(QRectF(0, 0, size, size), 22 * s, 22 * s)

    # Leaf: two curves from the stem (bottom left) to the tip (top right).
    leaf = QPainterPath(QPointF(24 * s, 78 * s))
    leaf.cubicTo(QPointF(20 * s, 40 * s), QPointF(45 * s, 20 * s), QPointF(80 * s, 20 * s))
    leaf.cubicTo(QPointF(80 * s, 55 * s), QPointF(60 * s, 80 * s), QPointF(24 * s, 78 * s))
    p.setBrush(LEAF)
    p.drawPath(leaf)

    # Midrib + stem
    p.setPen(QPen(BACKGROUND, 4 * s, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(16 * s, 86 * s), QPointF(68 * s, 32 * s))
    p.end()
    return image


def main() -> None:
    app = QGuiApplication(sys.argv)  # QPainter needs an application object for fonts/paint engine
    outputs = [
        (os.path.join(REPO, "robot", "web", "icon-192.png"), 192),
        (os.path.join(REPO, "robot", "web", "icon-512.png"), 512),
        (os.path.join(REPO, "desktop", "agriscout.ico"), 256),
    ]
    for path, size in outputs:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not draw(size).save(path):
            raise SystemExit(f"could not write {path}")
        print(f"wrote {path} ({size} px)")
    del app


if __name__ == "__main__":
    main()
