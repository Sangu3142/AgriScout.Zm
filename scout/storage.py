"""Storage conditions: the robot's DHT11 reading at scan time vs the crop's storage range.

The ranges are knowledge, not code: they live in data/conditions_seed.json ("storage", one entry
per crop, and "storage_rules"), copied with their sources from docs/storage_ranges_sources.md.
This file only compares. Nothing here touches a scan's status or the robot's LED: climate is
advice only.

  climate_columns(climate)  /api/status "climate" -> what a scan stores (NULL when not fresh)
  check(crop, temp, hum)    -> the card's "Storage conditions" section
"""

import json
from typing import List, Optional, Tuple

from scout import config

SENSOR_MAX_PCT = 90  # the DHT11 tops out at ~90-91 % RH: a higher reading means "at least 90"
SENSOR_NOTE = "Measured at the robot with a DHT11 (±2 °C, ±5 % RH)."

STORAGE_KEYS = {"crop", "temp_min_c", "temp_max_c", "temp_target", "temp_target_note",
                "within_text", "too_warm_text", "hot_above_c", "hot_text", "cool_min_c",
                "cool_text", "too_cold_text", "rh_min_pct", "rh_max_pct", "rh_target",
                "source_name", "source_url", "verified", "verified_note"}
NUMBER_KEYS = ("temp_min_c", "temp_max_c", "rh_min_pct")
OPTIONAL_NUMBER_KEYS = ("hot_above_c", "cool_min_c", "rh_max_pct")
SOURCE_KEYS = {"source_name", "source_url", "verified", "verified_note"}
RULES_KEYS = {"q10", "actions", "cool_chamber"}

# What the card shows for each result.
RESULT_TEXT = {"within": "Within range", "too_warm": "Too warm", "cool": "Cool",
               "too_cold": "Too cold", "too_dry": "Too dry", "too_humid": "Too humid",
               "sensor_max": "At least 90 %", "no_reading": "No recent reading"}


class StorageSeedError(ValueError):
    """The storage part of the seed file has a missing/unknown key or a bad value."""


# ---------------------------------------------------------------- knowledge base

def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_source(path: str, name: str, item: dict) -> None:
    if not isinstance(item.get("source_name"), str) or not item["source_name"].strip():
        raise StorageSeedError("%s: %s: source_name missing" % (path, name))
    if not isinstance(item.get("source_url"), str) or not item["source_url"].startswith("https://"):
        raise StorageSeedError("%s: %s: source_url must be an https:// link" % (path, name))
    if item.get("verified") not in (0, 1):
        raise StorageSeedError("%s: %s: verified must be 0 or 1" % (path, name))
    # verified=1 must say how and when the wording was checked against the source.
    note = item.get("verified_note")
    if item["verified"] == 1 and (not isinstance(note, str) or not note.strip()):
        raise StorageSeedError("%s: %s: verified=1 needs a verified_note (how and when "
                               "it was checked)" % (path, name))


def load_kb(path: str = config.CONDITIONS_SEED) -> dict:
    """{"storage": {crop: entry}, "rules": {...}} from the seed file. Raises StorageSeedError."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entries, rules = data.get("storage"), data.get("storage_rules")
    if not isinstance(entries, list) or not isinstance(rules, dict):
        raise StorageSeedError('%s: expected "storage": [...] and "storage_rules": {...}' % path)
    storage = {}
    for i, entry in enumerate(entries):
        name = entry.get("crop", "storage item %d" % i) if isinstance(entry, dict) else str(i)
        if not isinstance(entry, dict):
            raise StorageSeedError("%s: storage item %d is not an object" % (path, i))
        missing, unknown = STORAGE_KEYS - set(entry), set(entry) - STORAGE_KEYS
        if missing or unknown:
            raise StorageSeedError("%s: %s: missing keys %s, unknown keys %s"
                                   % (path, name, sorted(missing), sorted(unknown)))
        for key in NUMBER_KEYS:
            if not _is_number(entry[key]):
                raise StorageSeedError("%s: %s: %s must be a number" % (path, name, key))
        for key in OPTIONAL_NUMBER_KEYS:
            if entry[key] is not None and not _is_number(entry[key]):
                raise StorageSeedError("%s: %s: %s must be a number or null" % (path, name, key))
        if entry["temp_min_c"] >= entry["temp_max_c"]:
            raise StorageSeedError("%s: %s: temp_min_c must be below temp_max_c" % (path, name))
        _check_source(path, name, entry)
        storage[entry["crop"]] = entry

    if set(rules) != RULES_KEYS:
        raise StorageSeedError("%s: storage_rules must have exactly %s" % (path, sorted(RULES_KEYS)))
    q10, chamber = rules["q10"], rules["cool_chamber"]
    if (not isinstance(q10, dict) or set(q10) != {"low", "high"} | SOURCE_KEYS
            or not _is_number(q10["low"]) or not _is_number(q10["high"])):
        raise StorageSeedError("%s: storage_rules.q10 needs low, high and a source" % path)
    _check_source(path, "storage_rules.q10", q10)
    if not isinstance(chamber, dict) or set(chamber) != {"text"} | SOURCE_KEYS:
        raise StorageSeedError("%s: storage_rules.cool_chamber needs text and a source" % path)
    _check_source(path, "storage_rules.cool_chamber", chamber)
    if not isinstance(rules["actions"], list) or not all(isinstance(a, str)
                                                         for a in rules["actions"]):
        raise StorageSeedError("%s: storage_rules.actions must be a list of texts" % path)
    return {"storage": storage, "rules": rules}


# ---------------------------------------------------------------- reading at scan time

def climate_columns(climate: Optional[dict]) -> Tuple[Optional[float], Optional[float],
                                                      Optional[float]]:
    """(temp_c, humidity_pct, age_s) to store with a scan, or (None, None, None) unless the robot
    had a good reading at most config.CLIMATE_FRESH_S old. climate = /api/status "climate"."""
    if not isinstance(climate, dict) or climate.get("available") is not True:
        return None, None, None
    values = [climate.get(key) for key in ("temp_c", "humidity_pct", "age_s")]
    if not all(_is_number(v) for v in values) or values[2] > config.CLIMATE_FRESH_S:
        return None, None, None
    return float(values[0]), float(values[1]), float(values[2])


def humidity_text(humidity_pct: float) -> str:
    if humidity_pct >= SENSOR_MAX_PCT:
        return "≥ %d %% RH" % SENSOR_MAX_PCT
    return "%.0f %% RH" % humidity_pct


def reading_text(temp_c: Optional[float], humidity_pct: Optional[float]) -> str:
    """'28.6 °C · 57 % RH', or '' when there is no reading (field report)."""
    if temp_c is None or humidity_pct is None:
        return ""
    return "%.1f °C · %s" % (temp_c, humidity_text(humidity_pct))


# ---------------------------------------------------------------- comparison

def _half(value: float) -> str:
    """Rounded to the nearest 0.5: 2.46 -> '2.5', 4.17 -> '4', 9.49 -> '9.5'."""
    return "%g" % (round(value * 2) / 2)


def q10_text(temp_c: float, top_c: float, q10: dict) -> str:
    """FAO: spoilage speeds up 2-3x per 10 °C. An estimate over a wide span: says so."""
    steps = (temp_c - top_c) / 10.0
    return ("Spoils about %s-%sx faster than at %g °C (estimate, FAO: %g-%gx per 10 °C)"
            % (_half(q10["low"] ** steps), _half(q10["high"] ** steps), top_c,
               q10["low"], q10["high"]))


def _temperature(temp_c: float, s: dict, rules: dict) -> Tuple[str, List[str], str]:
    """(result, lines, q10 line)."""
    if temp_c > s["temp_max_c"]:
        lines = [s["too_warm_text"]] if s["too_warm_text"] else []
        if s["hot_above_c"] is not None and temp_c > s["hot_above_c"]:
            lines.append(s["hot_text"])
        return "too_warm", lines, q10_text(temp_c, s["temp_max_c"], rules["q10"])
    if temp_c >= s["temp_min_c"]:
        return "within", [s["within_text"]] if s["within_text"] else [], ""
    if s["cool_min_c"] is not None and temp_c >= s["cool_min_c"]:
        return "cool", [s["cool_text"]], ""
    return "too_cold", [s["too_cold_text"]], ""


def _humidity(humidity_pct: float, s: dict) -> Tuple[str, List[str]]:
    above_sensor = s["rh_max_pct"] is None or s["rh_max_pct"] > SENSOR_MAX_PCT
    if humidity_pct >= SENSOR_MAX_PCT and above_sensor:
        if s["rh_max_pct"] is None:
            line = "At least 90 %% - the DHT11 can't confirm the > %g %% target." % s["rh_min_pct"]
        else:
            line = "At least 90 % - the DHT11 can't confirm the target range."
        return "sensor_max", [line]
    if humidity_pct < s["rh_min_pct"]:
        return "too_dry", []
    if s["rh_max_pct"] is not None and humidity_pct > s["rh_max_pct"]:
        return "too_humid", []
    return "within", []


def _source(item: dict) -> dict:
    return {"name": item["source_name"], "url": item["source_url"],
            "verified": bool(item["verified"])}


def check(crop: Optional[str], temp_c: Optional[float], humidity_pct: Optional[float],
          age_s: Optional[float] = None, kb: Optional[dict] = None) -> dict:
    """The card's "Storage conditions" section.

    state: "no_reading" (nothing fresh stored with the scan), "crop_unknown" (reading shown,
    nothing to compare with) or "compared".
    """
    section = {"state": "no_reading", "reading": "", "age": "", "temp": None, "humidity": None,
               "q10": "", "actions": [], "cool_chamber": "", "sources": [],
               "note": SENSOR_NOTE, "no_reading_text": RESULT_TEXT["no_reading"]}
    if temp_c is None or humidity_pct is None:
        return section
    section["reading"] = reading_text(temp_c, humidity_pct)
    if age_s is not None:
        section["age"] = "measured %.0f s before the scan" % age_s
    kb = kb if kb is not None else load_kb()
    s = kb["storage"].get(crop) if crop else None
    if s is None:
        section["state"] = "crop_unknown"
        return section

    rules = kb["rules"]
    temp_result, temp_lines, q10 = _temperature(temp_c, s, rules)
    hum_result, hum_lines = _humidity(humidity_pct, s)
    target_lines = [s["temp_target_note"]] if s["temp_target_note"] else []
    section.update(
        state="compared",
        temp={"result": temp_result, "label": RESULT_TEXT[temp_result],
              "target": s["temp_target"], "lines": target_lines + temp_lines},
        humidity={"result": hum_result, "label": RESULT_TEXT[hum_result],
                  "target": s["rh_target"], "lines": hum_lines},
        q10=q10,
        sources=[_source(s)])
    if q10:
        section["sources"].append(_source(rules["q10"]))
    if temp_result == "too_warm" or hum_result == "too_dry":
        section["actions"] = list(rules["actions"])
        section["cool_chamber"] = rules["cool_chamber"]["text"]
        section["sources"].append(_source(rules["cool_chamber"]))
    return section
