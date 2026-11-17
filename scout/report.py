"""Field report: summary tiles + one card per batch with its latest scan, as HTML pages.

One function, two uses:
  - scout server GET /report       image_base="/"  (photos at /scans/<file>, cards at /cards/<id>.html)
  - GUI "Open field report"        write_report() -> data/report.html + data/cards/<id>.html,
                                   image_base="" (photos are next to it in data/scans/)
Each batch card has a "View" link to that scan's full result card (scout/card.py, same template
as the desktop result window).
Offline: inline CSS, no CDN, no JavaScript. Jinja autoescape is on, so text from the database
(notes, a strange QR ID) is shown as text, never run as HTML.
"""

import datetime
import os

from scout import config, db, storage
from scout.card import (ICONS, NO_PRODUCE_HEADING, STATUS_SHORT, STATUS_TEXT, STUB_BANNER,
                        UNCERTAIN_HEADING, card_data, crop_name, env, render_card)
from scout.classifier import is_stub

# Summary tiles, in this order. "Check" (watch) and "No tomato or green pepper found"
# (no_produce) only appear when a scan has that status.
TILE_ORDER = ("healthy", "watch", "action", "uncertain", "no_produce", "none")
ALWAYS_SHOWN_TILES = ("healthy", "action", "uncertain", "none")


def report_rows(conn) -> list:
    """[{plant, scan, condition, scans}]: every batch in plants.json, then ids seen only on a QR
    code (or old batches, e.g. maize before Phase 5c) that have scans, then 'unknown'.

    A plant that is not in plants.json and was never scanned is left out, so it doesn't count
    as "Not scanned".
    """
    latest = db.latest_scan_per_plant(conn)
    counts = dict(conn.execute("SELECT plant_id, COUNT(*) FROM scans GROUP BY plant_id"))
    plants = [p for p in db.list_plants(conn) if p["crop"] is not None or p["id"] in latest]
    if db.UNKNOWN_PLANT in latest:
        plants.append({"id": db.UNKNOWN_PLANT, "crop": None, "row": None,
                       "label": "no QR code read", "notes": ""})
    rows = []
    for plant in plants:
        scan = latest.get(plant["id"])
        condition = None
        if scan and scan["top1_condition_id"]:
            condition = db.get_condition(conn, scan["top1_condition_id"])
        rows.append({"plant": plant, "scan": scan, "condition": condition,
                     "scans": counts.get(plant["id"], 0)})
    return rows


def _short_title(row: dict) -> str:
    scan, condition = row["scan"], row["condition"]
    if scan is None:
        return ""
    if scan["status"] == "uncertain":
        return UNCERTAIN_HEADING  # never the guess
    if scan["status"] == "no_produce":
        return NO_PRODUCE_HEADING
    if condition:
        return condition["name"]
    return scan["top1_condition_id"] or "No diagnosis"


def render_report(db_path: str = config.DB_PATH, image_base: str = "/",
                  now: datetime.datetime = None) -> str:
    conn = db.open_db(db_path)
    try:
        rows = report_rows(conn)
    finally:
        conn.close()
    totals = {key: 0 for key in TILE_ORDER}
    for row in rows:
        totals[row["scan"]["status"] if row["scan"] else "none"] += 1
    tiles = [key for key in TILE_ORDER if key in ALWAYS_SHOWN_TILES or totals[key]]
    for row in rows:
        row["title"] = _short_title(row)
        row["crop_name"] = crop_name(row["plant"]["crop"])
        scan = row["scan"]  # climate at scan time ('' when none was stored)
        row["climate"] = storage.reading_text(scan["climate_temp_c"],
                                              scan["climate_humidity_pct"]) if scan else ""
    models = sorted({row["scan"]["model_name"] for row in rows if row["scan"]})
    now = now or datetime.datetime.now()
    return env.get_template("report.html").render(
        rows=rows, totals=totals, tiles=tiles, models=models, image_base=image_base,
        stub_used=any(is_stub(name) for name in models), stub_banner=STUB_BANNER, icons=ICONS,
        status_text=STATUS_TEXT, status_short=STATUS_SHORT, generated=now.strftime("%Y-%m-%d %H:%M"))


def write_cards(db_path: str = config.DB_PATH, cards_dir: str = config.CARDS_DIR) -> list:
    """data/cards/<scan_id>.html for the latest scan of every report row. Returns the paths."""
    conn = db.open_db(db_path)
    try:
        cards = [card_data(conn, row["scan"]["id"]) for row in report_rows(conn) if row["scan"]]
    finally:
        conn.close()
    os.makedirs(cards_dir, exist_ok=True)
    paths = []
    for data in cards:
        path = os.path.join(cards_dir, "%d.html" % data["scan_id"])
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_card(data, "report", image_base="../", back_href="../report.html"))
        paths.append(path)
    return paths


def write_report(db_path: str = config.DB_PATH, path: str = config.REPORT_PATH) -> str:
    """Write the report (+ its cards) next to data/scans/ so the links work from the file.
    Returns the report path."""
    write_cards(db_path, os.path.join(os.path.dirname(os.path.abspath(path)), "cards"))
    html = render_report(db_path, image_base="")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path
