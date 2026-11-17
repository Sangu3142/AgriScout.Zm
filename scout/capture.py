"""Collect training photos from the robot camera: one folder per model class.

Usage (repo root, laptop on the robot hotspot):
  python -m scout.capture --class tomato_fresh
  python -m scout.capture --class pepper_spoiled --robot 192.168.50.1
  python -m scout.capture --class background

"background" = no tomato or green pepper in view: the empty tray, the backdrop, hands, the QR
label alone, other fruit (apples, oranges), other red/green objects. It teaches the model to say
"no tomato or green pepper found" instead of guessing a crop.
At the end the exact command for each of the 5 classes is printed.

A small window shows the live stream. Keys:
  SPACE   save one photo           -> dataset/<class>/<YYYYmmdd-HHMMSS-mmm>.jpg
  B       burst: 10 photos, 0.3 s apart (turn the fruit meanwhile)
  Q / Esc quit
Then `python -m scout.zip_dataset` packs dataset/ for Colab.

The preview is only for aiming. Saved photos come from the snapshot URL, byte for byte, which is
exactly what a scan sees (scout/pipeline.py): training and scanning photos then match.
This runs on the laptop, so opening the stream URL here does not touch the Pi's /dev/video0.
"""

import argparse
import datetime
import os
import threading
import time
from typing import Callable, Optional

import cv2
import numpy as np

from scout import config
from scout.classifier import MODEL_CLASSES
from scout.pipeline import ScanError, grab_snapshot

BURST_COUNT = 10
BURST_GAP_S = 0.3
PREVIEW_WIDTH = 480
STREAM_TIMEOUT_MS = 3000
WINDOW = "AgriScout.Zm capture"


def photo_path(out_dir: str, cls: str, now: datetime.datetime) -> str:
    """dataset/<class>/20260928-153012-345.jpg; _2, _3 ... if that name is taken."""
    folder = os.path.join(out_dir, cls)
    base = now.strftime("%Y%m%d-%H%M%S-") + "%03d" % (now.microsecond // 1000)
    path, n = os.path.join(folder, base + ".jpg"), 2
    while os.path.exists(path):
        path, n = os.path.join(folder, "%s_%d.jpg" % (base, n)), n + 1
    return path


def count_photos(folder: str) -> int:
    if not os.path.isdir(folder):
        return 0
    return sum(1 for name in os.listdir(folder) if name.lower().endswith(".jpg"))


class Saver:
    """Saves snapshots for one class, one job at a time (a photo or a burst) in a thread,
    so the preview keeps moving. The GUI loop only reads the status fields."""

    def __init__(self, out_dir: str, cls: str, get_jpeg: Callable[[], bytes]):
        self.out_dir, self.cls, self.get_jpeg = out_dir, cls, get_jpeg
        self.saved = 0              # this session
        self.burst_done = 0         # photos of the running burst (0 = no burst)
        self.message = ""           # last error, shown on the preview
        self._thread: Optional[threading.Thread] = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def save_one(self) -> Optional[str]:
        """One snapshot to a new file. Returns its path, or None (message says why)."""
        try:
            jpeg = self.get_jpeg()
        except ScanError as err:
            self.message = str(err)
            return None
        path = photo_path(self.out_dir, self.cls, datetime.datetime.now())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(jpeg)
        self.saved += 1
        self.message = ""
        return path

    def burst(self, count: int = BURST_COUNT, gap_s: float = BURST_GAP_S) -> None:
        """count photos, one every gap_s seconds counted from the start. If a snapshot is slow
        (robot Wi-Fi), the next one starts straight away; the burst never catches up faster."""
        start = time.monotonic()
        for i in range(count):
            delay = start + i * gap_s - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.burst_done = i + 1
            self.save_one()
        self.burst_done = 0

    def start(self, job: Callable[[], object]) -> bool:
        """Run save_one or burst in the background. False if a job is still running."""
        if self.busy:
            return False
        self._thread = threading.Thread(target=job, daemon=True)
        self._thread.start()
        return True

    def wait(self, timeout: float = 10.0) -> None:
        """Let a running job finish (quit during a burst must not leave half a JPEG)."""
        if self._thread is not None:
            self._thread.join(timeout)


class LatestFrame(threading.Thread):
    """Reads the MJPEG stream and keeps only the newest frame (no backlog, like gui.VideoThread)."""

    def __init__(self, url: str):
        super().__init__(daemon=True)
        self.url = url
        self.frame: Optional[np.ndarray] = None
        self.stopping = False

    def run(self) -> None:
        while not self.stopping:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG,
                                   [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, STREAM_TIMEOUT_MS,
                                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, STREAM_TIMEOUT_MS])
            while not self.stopping and cap.isOpened():
                ok, frame = cap.read()
                if not ok:
                    break
                self.frame = frame  # one reference swap: the GUI loop sees old or new, never half
            cap.release()
            self.frame = None
            if not self.stopping:
                time.sleep(1.0)  # robot off or stream dropped: retry


def draw_overlay(frame: Optional[np.ndarray], lines: list) -> np.ndarray:
    """Preview scaled to PREVIEW_WIDTH, with status text; a black frame when there is no video."""
    if frame is None:
        view = np.zeros((360, PREVIEW_WIDTH, 3), np.uint8)
        lines = lines + ["no video stream (photos can still be saved)"]
    else:
        height = frame.shape[0] * PREVIEW_WIDTH // frame.shape[1]
        view = cv2.resize(frame, (PREVIEW_WIDTH, height))
    for i, text in enumerate(lines):
        y = 22 + 22 * i
        # black outline then white text: readable on any background
        cv2.putText(view, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(view, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1,
                    cv2.LINE_AA)
    return view


def capture_commands(robot: str = config.DEFAULT_ROBOT_HOST) -> list:
    """The command to type for each model class ("--robot" only if it isn't the default)."""
    extra = "" if robot == config.DEFAULT_ROBOT_HOST else " --robot %s" % robot
    return ["python -m scout.capture --class %s%s" % (cls, extra) for cls in MODEL_CLASSES]


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Save training photos from the robot camera")
    parser.add_argument("--class", dest="cls", required=True, choices=MODEL_CLASSES,
                        help="which folder the photos go to")
    parser.add_argument("--robot", default=config.DEFAULT_ROBOT_HOST, help="robot host")
    parser.add_argument("--out", default=config.DATASET_DIR, help="dataset folder")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> None:
    args = parse_args(argv)
    snapshot_url = config.snapshot_url_for(args.robot)
    saver = Saver(args.out, args.cls, lambda: grab_snapshot(snapshot_url))
    folder = os.path.join(args.out, args.cls)
    stream = LatestFrame(config.stream_url_for(args.robot))
    stream.start()
    print("class %s -> %s" % (args.cls, folder))
    print("SPACE = photo, B = burst of %d, Q = quit" % BURST_COUNT)
    try:
        while True:
            lines = ["class: %s" % args.cls,
                     "saved %d now / %d in folder" % (saver.saved, count_photos(folder))]
            if saver.burst_done:
                lines.append("BURST %d/%d" % (saver.burst_done, BURST_COUNT))
            elif saver.busy:
                lines.append("saving...")
            if saver.message:
                lines.append(saver.message[:60])
            cv2.imshow(WINDOW, draw_overlay(stream.frame, lines))
            key = cv2.waitKey(30) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break
            if key == ord(" "):
                saver.start(saver.save_one)
            elif key in (ord("b"), ord("B")):
                saver.start(saver.burst)
            if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:  # window closed with X
                break
    finally:
        stream.stopping = True
        cv2.destroyAllWindows()
        saver.wait()
    print("saved %d photos, %d in %s" % (saver.saved, count_photos(folder), folder))
    print("Commands for each class:")
    for command in capture_commands(args.robot):
        print("  " + command)


if __name__ == "__main__":
    main()
