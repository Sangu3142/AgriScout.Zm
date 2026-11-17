"""The scan result card: one HTML template (templates/card.html), used by
  - the desktop result window (desktop/gui.py, QWebEngineView)          mode "app"
  - the field report's "View" pages (data/cards/<id>.html, GET /cards/)  mode "report"
  - "Save / Share" (the card as a PNG)                                  mode "export"

card_data() builds everything the card shows from the database row of the scan, so the GUI and
the report can never show different things for the same scan.

Status labels and the "uncertain" wording live here too; report.py and gui.py import them.
"""

import datetime
import os
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from scout import config, db, storage
from scout.classifier import BACKGROUND, BACKGROUND_NAME, is_stub

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
# autoescape: text from the database (notes, a strange QR ID) is shown as text, never run as HTML.
env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))

# Words for the produce use case. The database keeps the old status names (healthy/watch/...).
STATUS_TEXT = {"healthy": "Good", "watch": "Check", "action": "Remove",
               "uncertain": "Not sure - scan again",
               "no_produce": "No tomato or green pepper found", "none": "Not scanned"}
# Small chips (report grid): same words, but short enough to sit next to the batch ID.
STATUS_SHORT = dict(STATUS_TEXT, uncertain="Not sure", no_produce="No tomato/pepper")

# An "uncertain" scan never shows its top-1 guess as a diagnosis, nor that condition's advice:
# someone could act on a guess.
UNCERTAIN_HEADING = "Not sure - scan again"
UNCERTAIN_ADVICE = ("Move closer, hold the fruit steady in good light, turn the damaged side to "
                    "the camera and scan again. If still unsure, check it by eye or ask the camp "
                    "extension officer.")
# "no_produce": the model's top result is "background". No percentage, no advice.
# Not "no produce": background also holds other fruit (apples, oranges) and red/green objects.
NO_PRODUCE_HEADING = "No tomato or green pepper found"
NO_PRODUCE_TEXT = ("This doesn't look like a tomato or a green pepper. Place one in front of the "
                   "camera and scan again.")

# The stub ignores the photo: say it loudly on every card (and in the GUI) so nobody takes a
# scripted answer for a reading of the camera image.
STUB_BANNER = "STUB - not using the camera image"

MODES = ("app", "report", "export")

# In the desktop window the card's buttons are links to this address; gui.py catches them before
# they load (a custom "agriscout:" scheme never reaches Qt: Chromium drops it). ".invalid" is a
# reserved domain (RFC 2606), so even if a click slipped through it could not reach any server.
ACTION_BASE = "https://agriscout.invalid/"

# (condition key, title, icon) in the order the card shows them.
ADVICE_SECTIONS = (("what_to_do_now", "Do now", "warning"),
                   ("symptoms", "Symptoms", "eye"),
                   ("prevention", "Prevention", "shield"),
                   ("when_to_get_help", "Get help when", "phone"))


def _svg(body: str, fill: str = "none") -> Markup:
    """A 24x24 line icon. Markup = trusted HTML (these strings are ours, not from the database)."""
    return Markup('<svg viewBox="0 0 24 24" width="24" height="24" fill="%s" stroke="currentColor" '
                  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
                  'aria-hidden="true">%s</svg>' % (fill, body))


ICONS = {
    "back": _svg('<path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/>'),
    "leaf": _svg('<path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.5 19 2c1 2 2 4.2 2 8 0 5.5-4.8 '
                 '10-10 10Z"/><path d="M2 21c0-3 1.9-5.4 5.1-6"/>'),
    "clock": _svg('<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>'),
    "flask": _svg('<path d="M9 3h6"/><path d="M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 '
                  '0 0 1.3-2L14 9V3"/><path d="M7 15h10"/>'),
    "warning": _svg('<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 '
                    '0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>'),
    "eye": _svg('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/>'
                '<circle cx="12" cy="12" r="3"/>'),
    "shield": _svg('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>'),
    "phone": _svg('<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 '
                  '1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 '
                  '2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 '
                  '2.7.7a2 2 0 0 1 1.7 2Z"/>'),
    "share": _svg('<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/>'
                  '<circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4"/><path d="M15.4 6.5l-6.8 4"/>'),
    "camera": _svg('<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 '
                   '0 0 0-2-2h-3l-2.5-3Z"/><circle cx="12" cy="13" r="3"/>'),
    "thermo": _svg('<path d="M14 14.8V4a2 2 0 0 0-4 0v10.8a4 4 0 1 0 4 0Z"/>'),
    # crop tiles
    "tomato": _svg('<circle cx="12" cy="14" r="7"/><path d="M12 7V4"/>'
                   '<path d="M8.5 7.5 12 9l3.5-1.5"/>'),
    "pepper": _svg('<path d="M12 8c-3.5 0-5 2.5-5 6 0 4 2.5 8 5 8s5-4 5-8c0-3.5-1.5-6-5-6Z"/>'
                   '<path d="M12 8c0-2.5 1-4 3.5-4.5"/>'),
    "crop_unknown": _svg('<path d="M12 21c-4-3-6-6.5-6-10a6 6 0 0 1 12 0c0 3.5-2 7-6 10Z"/>'
                         '<path d="M12 21V9"/>'),
}


def best_guess_text(name: Optional[str], confidence: Optional[float]) -> str:
    """'best guess: Spoiled tomato 52 %', or '' when there was no prediction at all."""
    if not name:
        return ""
    if confidence is None:
        return "best guess: %s" % name
    return "best guess: %s %.0f %%" % (name, confidence * 100)


def crop_name(crop: Optional[str]) -> str:
    return db.CROP_NAMES.get(crop, "Unknown crop") if crop else "Unknown crop"


def when_text(timestamp: str) -> str:
    """'2026-09-28T15:42:07' -> '28 Sep 2026 · 15:42' (the stored text if it doesn't parse)."""
    try:
        t = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return timestamp or ""
    return "%d %s · %s" % (t.day, t.strftime("%b %Y"), t.strftime("%H:%M"))


def _kind(condition: Optional[dict]) -> str:
    """Dot colour in the top-3 list: fresh (green), spoiled (red), unknown (grey)."""
    if condition is None:
        return "unknown"
    return "fresh" if condition["type"] == "healthy" else "spoiled"


def card_data(conn, scan_id: int) -> Optional[dict]:
    """Everything the card shows, for one scan. None if there is no such scan.

    Works on old rows too (e.g. maize scans from before Phase 5c): a condition that is no longer
    in the knowledge base shows its raw id, with no advice.
    """
    scan = db.get_scan(conn, scan_id)
    if scan is None:
        return None
    plant = db.get_plant(conn, scan["plant_id"]) if scan["plant_id"] != db.UNKNOWN_PLANT else None
    top1 = scan["top1_condition_id"]
    condition = db.get_condition(conn, top1) if top1 else None
    status = scan["status"]
    uncertain = status == "uncertain"
    no_produce = status == "no_produce"
    confident = condition is not None and not uncertain and not no_produce

    crop = plant["crop"] if plant else None
    if crop is None and confident:
        crop = condition["crop"]  # no stake read, but a confident diagnosis says which crop

    if scan["plant_id"] == db.UNKNOWN_PLANT:
        chip = "No QR code"
    elif plant and plant["row"]:
        chip = "%s · row %s" % (scan["plant_id"], plant["row"])
    else:
        chip = scan["plant_id"]

    name = condition["name"] if condition else top1
    confidence = scan["confidence"]
    top3 = []
    for cid, conf in scan["top3"]:
        c = db.get_condition(conn, cid)
        shown = BACKGROUND_NAME if cid == BACKGROUND else (c["name"] if c else cid)
        top3.append({"name": shown, "percent": round(conf * 100), "kind": _kind(c)})

    advice = []
    if confident:
        advice = [{"title": title, "icon": icon, "text": condition[key]}
                  for key, title, icon in ADVICE_SECTIONS]
    verified = bool(advice and condition["verified"])
    # The source belongs to the advice: no advice shown (unsure, no produce), no source. So a
    # card without a known crop never shows e.g. the tomato source of a guess.
    if not advice:
        source = ""
    elif verified and condition["source_name"]:
        source = "Source: %s" % condition["source_name"]
    else:
        source = "Source to be verified"

    if no_produce:
        title, retake = NO_PRODUCE_HEADING, NO_PRODUCE_TEXT
    elif uncertain:
        title, retake = UNCERTAIN_HEADING, UNCERTAIN_ADVICE
    else:
        title, retake = name or "No diagnosis", ""

    return {
        "scan_id": scan["id"],
        "plant_id": scan["plant_id"],
        "status": status,
        "status_text": STATUS_TEXT[status],
        "chip": chip,
        "crop": crop,
        "crop_name": crop_name(crop),
        "crop_icon": crop if crop in db.CROPS else "crop_unknown",
        "when": when_text(scan["timestamp"]),
        "timestamp": scan["timestamp"],
        "image_path": scan["image_path"],
        "uncertain": uncertain,
        "no_produce": no_produce,
        "title": title,
        "guess": best_guess_text(name, confidence) if uncertain else "",
        # no produce: no percentage anywhere (not even the top 3)
        "percent": round(confidence * 100) if confidence is not None and not no_produce else None,
        "top3": [] if no_produce else top3,
        "advice": advice,
        "retake_advice": retake,
        "advice_verified": verified,
        "source": source,
        "brightness": scan["brightness"],
        # Robot's DHT11 reading at scan time vs the crop's storage range. Advice only: it never
        # changes the status, title or advice above. Like the advice, only on a confident card:
        # "Not sure" and "no tomato/pepper" cards show no advice, no numbers, no sources.
        "storage": storage.check(crop, scan["climate_temp_c"], scan["climate_humidity_pct"],
                                 scan["climate_age_s"])
                   if not uncertain and not no_produce else None,
        "notes": scan["notes"],
        "model_name": scan["model_name"],
        "stub": is_stub(scan["model_name"]),
        "stub_banner": STUB_BANNER,
    }


def export_name(data: dict) -> str:
    """File name for Save / Share: 'T-02_20260928-154207.png'."""
    safe = "".join(ch if ch.isalnum() or ch == "-" else "_" for ch in data["plant_id"])
    return "%s_%s.png" % (safe, data["timestamp"].replace("-", "").replace(":", "")
                          .replace("T", "-"))


def render_card(data: dict, mode: str = "app", image_base: str = "",
                back_href: Optional[str] = None) -> str:
    """The card as a full HTML page.

    image_base: prefix for data["image_path"] ("scans/x.jpg"): "" when the page's base is data/,
    "../" for data/cards/<id>.html, "/" on the scout server.
    back_href: where the back arrow goes in report mode (the arrow is hidden in export mode).
    """
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % ", ".join(MODES))
    if mode == "app":
        back_href = ACTION_BASE + "back"
    elif mode == "export":
        back_href = None
    return env.get_template("card.html").render(
        c=data, mode=mode, icons=ICONS, image_src=image_base + data["image_path"],
        back_href=back_href, action_base=ACTION_BASE,
        threshold=int(config.CONF_THRESHOLD * 100))
