"""One batch scan: snapshot -> QR batch ID -> classify -> advice -> save photo + DB row.

Used by the desktop GUI (in a worker thread), by the scout server (POST /scan) and from the
command line:
  python -m scout.pipeline                       snapshot from the robot (192.168.50.1)
  python -m scout.pipeline --image fruit.jpg     a photo file instead (offline testing)
  python -m scout.pipeline --image fruit.jpg --plant T-02

Only reads the camera snapshot; never drives the robot or moves a servo.
Stop the tank before scanning: a moving camera gives a blurred photo.
"""

import argparse
import datetime
import json
import os
import re
from typing import Optional, Tuple

import cv2
import numpy as np
import requests

from scout import config, db, storage
from scout.classifier import BACKGROUND, BACKGROUND_NAME, Classifier, load_classifier, scan_status
from scout.qr import decode_plant_id

DARK_BRIGHTNESS = 50  # mean HSV V (0-255) below this: note that the photo is dark


class ScanError(Exception):
    """The scan could not happen (no photo). Nothing was saved."""


def grab_snapshot(url: str, timeout: float = config.SNAPSHOT_TIMEOUT_S) -> bytes:
    """One JPEG from mjpg-streamer's snapshot URL."""
    try:
        reply = requests.get(url, timeout=timeout)
        reply.raise_for_status()
    except requests.Timeout as err:
        raise ScanError("no camera snapshot from %s (no answer within %.0f s)"
                        % (url, timeout)) from err
    except requests.ConnectionError as err:  # robot off, wrong address, mjpg-streamer not running
        raise ScanError("no camera snapshot from %s (cannot connect)" % url) from err
    except requests.RequestException as err:  # e.g. HTTP 500
        raise ScanError("no camera snapshot from %s (%s)" % (url, err)) from err
    return reply.content


def fetch_climate(robot_address: str, timeout: float = config.CLIMATE_TIMEOUT_S) -> Optional[dict]:
    """The robot's DHT11 climate (/api/status "climate"), or None. Never raises: a scan must work
    without it (robot server off, old version, Wi-Fi slow)."""
    try:
        reply = requests.get(config.status_url_for(robot_address), timeout=timeout)
        reply.raise_for_status()
        climate = reply.json().get("climate")
    except (requests.RequestException, ValueError, AttributeError):
        return None
    return climate if isinstance(climate, dict) else None


def decode_image(image_bytes: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ScanError("the camera sent something that is not a picture")
    return image


def brightness(image: np.ndarray) -> float:
    """Light level of the photo: mean of the HSV V channel, 0 (black) to 255 (white).
    The tank's light sensors are not fitted, so the camera measures light instead."""
    return float(cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[:, :, 2].mean())


def _photo_path(scans_dir: str, now: datetime.datetime, plant_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9-]", "_", plant_id)  # never let a QR text become a path
    base = "%s_%s" % (now.strftime("%Y%m%d-%H%M%S"), safe_id)
    path, n = os.path.join(scans_dir, base + ".jpg"), 2
    while os.path.exists(path):  # two scans in the same second
        path, n = os.path.join(scans_dir, "%s_%d.jpg" % (base, n)), n + 1
    return path


def crop_filter(conn, predictions: list, crop: Optional[str]) -> Tuple[list, list]:
    """Drop predictions for the other crop (a tomato batch can't be a spoiled pepper).

    Confidences are NOT re-scaled: if the model put most of its belief on the wrong crop, what's
    left is small and the scan honestly ends up "uncertain". Returns (kept, dropped ids).
    "background" (no produce) belongs to no crop: it is never dropped.
    """
    if not crop:
        return predictions, []
    kept, dropped = [], []
    for cid, conf in predictions:
        if cid == BACKGROUND:
            kept.append((cid, conf))
            continue
        condition = db.get_condition(conn, cid)
        if condition is not None and condition["crop"] != crop:
            dropped.append(cid)
        else:
            kept.append((cid, conf))
    return kept, dropped


def scan_plant(classifier: Classifier, db_path: str = config.DB_PATH,
               snapshot_url: Optional[str] = None, image_bytes: Optional[bytes] = None,
               plant_id: Optional[str] = None, now: Optional[datetime.datetime] = None,
               scans_dir: str = config.SCANS_DIR, climate: Optional[dict] = None) -> dict:
    """Scan one plant and return the result as a dict (JSON-ready). Raises ScanError if there
    is no usable photo; in that case nothing is saved.

    image_bytes: a JPEG/PNG to use instead of a snapshot. plant_id: skip the QR code.
    now: the scan time (tests pass a fixed one).
    climate: the robot's /api/status "climate" at scan time. Stored only if it is a good reading
    at most config.CLIMATE_FRESH_S old. It never changes the status: advice only.
    """
    climate_cols = storage.climate_columns(climate)
    if image_bytes is None:
        if not snapshot_url:
            raise ScanError("no snapshot URL and no image given")
        image_bytes = grab_snapshot(snapshot_url)
    image = decode_image(image_bytes)
    now = now or datetime.datetime.now()
    notes = []

    conn = db.open_db(db_path)
    try:
        # 1. Which plant?
        qr_found = False
        if not plant_id:
            plant_id = decode_plant_id(image)
            qr_found = plant_id is not None
            if not qr_found:
                plant_id = db.UNKNOWN_PLANT
                notes.append("no QR stake found in the photo")
        plant = db.get_plant(conn, plant_id) if plant_id != db.UNKNOWN_PLANT else None
        if plant is None and plant_id != db.UNKNOWN_PLANT:
            db.add_unknown_plant(conn, plant_id)
            plant = db.get_plant(conn, plant_id)
            notes.append("plant %s is not in plants.json" % plant_id)

        # 2. What does it have?
        predictions = classifier.predict(image, plant_id)
        predictions, dropped = crop_filter(conn, predictions, plant["crop"] if plant else None)
        if dropped:
            notes.append("ignored other-crop results: %s" % ", ".join(dropped))
        top1_id, top1_conf = predictions[0] if predictions else (None, None)
        condition = db.get_condition(conn, top1_id) if top1_id else None
        if top1_id and top1_id != BACKGROUND and condition is None:
            notes.append("%s is not in the knowledge base" % top1_id)
        status = scan_status(top1_id, condition, top1_conf)

        light = brightness(image)
        if light < DARK_BRIGHTNESS:
            notes.append("photo is dark (brightness %.0f of 255)" % light)

        # 3. Save the photo exactly as the camera sent it (no re-encoding), then the row.
        os.makedirs(scans_dir, exist_ok=True)
        photo = _photo_path(scans_dir, now, plant_id)
        with open(photo, "wb") as f:
            f.write(image_bytes)
        data_dir = os.path.dirname(os.path.abspath(scans_dir))
        image_path = os.path.relpath(photo, data_dir).replace(os.sep, "/")
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%S")
        note_text = "; ".join(notes) or None
        scan_id = db.insert_scan(conn, timestamp, plant_id, image_path, top1_id, top1_conf,
                                 predictions, classifier.name, status, light, note_text,
                                 climate_cols)

        names = {cid: BACKGROUND_NAME if cid == BACKGROUND
                 else (db.get_condition(conn, cid) or {}).get("name", cid)
                 for cid, _ in predictions}
    finally:
        conn.close()

    return {
        "ok": True,
        "scan_id": scan_id,
        "timestamp": timestamp,
        "plant_id": plant_id,
        "qr_found": qr_found,
        "plant": plant,
        "status": status,
        "condition_id": top1_id,
        "confidence": top1_conf,
        "top3": [{"id": cid, "name": names[cid], "confidence": conf}
                 for cid, conf in predictions],
        "condition": condition,
        "model_name": classifier.name,
        "brightness": round(light, 1),
        "climate": None if climate_cols[0] is None else dict(
            zip(("temp_c", "humidity_pct", "age_s"), climate_cols)),
        "image_path": image_path,
        "image_file": photo,
        "notes": note_text,
    }


def make_classifier(db_path: str = config.DB_PATH) -> Classifier:
    """The classifier to use (ONNX model if present, else the stub), checked against the
    knowledge base in db_path. Call after db.setup()."""
    conn = db.open_db(db_path)
    try:
        known = db.condition_ids(conn)
    finally:
        conn.close()
    return load_classifier(known_ids=known)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan one batch")
    parser.add_argument("--robot", default=config.DEFAULT_ROBOT_HOST, help="robot host")
    parser.add_argument("--image", help="use this photo file instead of the robot camera")
    parser.add_argument("--plant", help="batch ID, e.g. T-02 (skip reading the QR code)")
    args = parser.parse_args()

    db.setup()
    image_bytes = None
    if args.image:
        with open(args.image, "rb") as f:
            image_bytes = f.read()
    classifier = make_classifier()
    # A photo file is not from here and now: no climate for it.
    climate = None if args.image else fetch_climate(args.robot)
    try:
        result = scan_plant(classifier, snapshot_url=config.snapshot_url_for(args.robot),
                            image_bytes=image_bytes, plant_id=args.plant, climate=climate)
    except ScanError as err:
        raise SystemExit("scan failed: %s" % err)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
