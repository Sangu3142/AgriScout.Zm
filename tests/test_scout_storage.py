"""DHT11 phase 3: storage ranges (scout/storage.py), climate at scan time, card + report."""

import copy
import json
import re
import sqlite3

import pytest

from scout import card, config, db, report, storage
from scout.pipeline import fetch_climate, scan_plant
from test_scout_db import CHEMICAL_RE, DOSE_RE
from test_scout_pipeline import NOW, FixedClassifier, photo

KB = storage.load_kb()
FRESH = {"available": True, "temp_c": 28.6, "humidity_pct": 57.0, "age_s": 4.0, "ok": 9,
         "failed": 0, "last_error": None, "reason": None}


def check(crop, temp, hum, kb=KB):
    return storage.check(crop, temp, hum, 4.0, kb)


# ---------------------------------------------------------------- knowledge base

def test_seed_storage_numbers_from_the_sources_doc():
    tomato, pepper = KB["storage"]["tomato"], KB["storage"]["pepper"]
    assert (tomato["temp_min_c"], tomato["temp_max_c"], tomato["cool_min_c"]) == (12.5, 15, 10)
    assert (tomato["rh_min_pct"], tomato["rh_max_pct"], tomato["hot_above_c"]) == (90, 95, 25)
    assert (pepper["temp_min_c"], pepper["temp_max_c"]) == (5, 7.5)
    assert (pepper["rh_min_pct"], pepper["rh_max_pct"]) == (95, None)
    assert set(KB["storage"]) == set(db.CROPS)
    q10 = KB["rules"]["q10"]
    assert (q10["low"], q10["high"]) == (2, 3)


def test_seed_verified_flags_and_notes():
    # 5 Oct: FAO checked online; UC Davis (blocks automated fetch) and the AJFAND PDF checked by
    # hand in a browser. Every verified source says how and when.
    sources = [KB["storage"]["tomato"], KB["storage"]["pepper"], KB["rules"]["q10"],
               KB["rules"]["cool_chamber"]]
    for src in sources:
        assert src["verified"] == 1 and "2026-10-05" in src["verified_note"]
    for src in sources[:2] + sources[3:]:
        assert src["verified_note"].startswith("checked by hand in a browser on 2026-10-05")


def test_cool_chamber_wording_matches_the_paper():
    text = KB["rules"]["cool_chamber"]["text"]
    assert "Ethiopia (Amha et al. 2025)" in text
    assert "mean maximum temperature was 5.7-6.8 °C lower" in text
    assert "(measured while storing kale)" in text
    assert "(the room: at most about 40-45 %)" in text
    assert "16 to 24 days in Addis Ababa and 12 to 22 days in Woliso" in text
    assert "(room storage to cool chamber)" in text


def test_verified_source_needs_a_note(tmp_path):
    data = seed_data()
    data["storage"][0]["verified_note"] = ""
    with pytest.raises(storage.StorageSeedError, match="verified=1 needs a verified_note"):
        storage.load_kb(write(tmp_path, data))
    data["storage"][0]["verified"] = 0                    # unverified: a note is not required
    storage.load_kb(write(tmp_path, data))


def test_storage_seed_has_no_doses_or_chemicals():
    with open(config.CONDITIONS_SEED, encoding="utf-8") as f:
        data = json.load(f)
    text = json.dumps([data["storage"], data["storage_rules"]], ensure_ascii=False)
    assert not DOSE_RE.search(text) and not CHEMICAL_RE.search(text)


def write(tmp_path, data):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def seed_data():
    with open(config.CONDITIONS_SEED, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("break_it, message", [
    (lambda d: d["storage"][0].pop("rh_target"), "missing keys"),
    (lambda d: d["storage"][0].update(temp_max="x"), "unknown keys"),
    (lambda d: d["storage"][0].update(temp_min_c="cold"), "must be a number"),
    (lambda d: d["storage"][0].update(temp_min_c=20), "below temp_max_c"),
    (lambda d: d["storage"][1].update(source_url="http://x"), "https://"),
    (lambda d: d["storage_rules"]["q10"].pop("source_name"), "q10"),
    (lambda d: d.pop("storage_rules"), "storage_rules"),
])
def test_bad_storage_seed_gives_clear_error(tmp_path, break_it, message):
    data = seed_data()
    break_it(data)
    with pytest.raises(storage.StorageSeedError, match=re.escape(message)):
        storage.load_kb(write(tmp_path, data))


def test_setup_checks_the_storage_seed(tmp_path):
    data = seed_data()
    data["storage"][0]["temp_min_c"] = "cold"
    with pytest.raises(storage.StorageSeedError):
        db.setup(str(tmp_path / "a.db"), write(tmp_path, data))


# ---------------------------------------------------------------- comparison

def test_tomato_within_range():
    s = check("tomato", 13.0, 92.0)
    assert s["state"] == "compared"
    assert (s["temp"]["result"], s["temp"]["label"]) == ("within", "Within range")
    assert s["temp"]["target"] == "12.5-15 °C for mature-green fruit"
    assert "Ripe (firm-ripe) fruit: 7-10 °C for 3-5 days only." in s["temp"]["lines"]
    assert s["q10"] == "" and s["actions"] == []


def test_tomato_too_warm_q10_against_15():
    s = check("tomato", 28.0, 57.0)
    assert s["temp"]["result"] == "too_warm" and s["humidity"]["result"] == "too_dry"
    assert s["q10"] == "Spoils about 2.5-4x faster than at 15 °C (estimate, FAO: 2-3x per 10 °C)"
    assert any("above 25 °C" in line for line in s["temp"]["lines"])   # 28 > 25: ripening note
    assert s["actions"] and "Ethiopia (Amha et al. 2025)" in s["cool_chamber"]


def test_tomato_warm_but_below_25_has_no_ripening_note():
    s = check("tomato", 20.0, 92.0)
    assert s["temp"]["result"] == "too_warm"
    assert not any("25 °C" in line for line in s["temp"]["lines"])


def test_tomato_31_degrees():
    assert check("tomato", 31.0, 50.0)["q10"].startswith("Spoils about 3-6x faster than at 15 °C")


def test_pepper_too_warm_q10_against_7_5():
    s = check("pepper", 28.0, 57.0)
    assert s["q10"] == ("Spoils about 4-9.5x faster than at 7.5 °C "
                        "(estimate, FAO: 2-3x per 10 °C)")
    assert "Peppers stored above 7.5 °C lose more water and shrivel." in s["temp"]["lines"]
    assert check("pepper", 31.0, 57.0)["q10"].startswith("Spoils about 5-13x")


@pytest.mark.parametrize("temp", [10.0, 11.0, 12.4])
def test_tomato_cool_between_10_and_12_5(temp):
    s = check("tomato", temp, 92.0)
    assert (s["temp"]["result"], s["temp"]["label"]) == ("cool", "Cool")
    assert ("Below the range for green (mature-green) fruit; fine for ripe fruit for 3-5 days."
            in s["temp"]["lines"])
    assert not any("Chilling" in line for line in s["temp"]["lines"])


def test_tomato_too_cold_below_10_with_chilling_warning():
    s = check("tomato", 9.9, 92.0)
    assert s["temp"]["result"] == "too_cold"
    assert any(line.startswith("Chilling injury") for line in s["temp"]["lines"])


def test_pepper_within_and_too_cold():
    s = check("pepper", 6.0, 92.0)
    assert s["temp"]["result"] == "within"
    assert s["temp"]["lines"] == ["OK for up to 2 weeks; 7.5 °C gives the longest shelf life "
                                  "(3-5 weeks)."]
    assert check("pepper", 7.5, 92.0)["temp"]["result"] == "within"
    s = check("pepper", 4.9, 92.0)
    assert s["temp"]["result"] == "too_cold"
    assert s["temp"]["lines"] == ["Too cold - chilling injury risk."]


def test_too_dry():
    s = check("tomato", 13.0, 89.0)
    assert (s["humidity"]["result"], s["humidity"]["label"]) == ("too_dry", "Too dry")
    assert s["actions"]                  # dry air: the low-cost actions (damp sack...)


@pytest.mark.parametrize("hum", [90.0, 91.0])
def test_humidity_cap_tomato(hum):
    s = check("tomato", 13.0, hum)
    assert (s["humidity"]["result"], s["humidity"]["label"]) == ("sensor_max", "At least 90 %")
    assert s["humidity"]["lines"] == ["At least 90 % - the DHT11 can't confirm the target range."]


def test_humidity_cap_pepper():
    s = check("pepper", 6.0, 90.0)
    assert s["humidity"]["result"] == "sensor_max"
    assert s["humidity"]["lines"] == ["At least 90 % - the DHT11 can't confirm the > 95 % target."]
    assert check("pepper", 6.0, 89.0)["humidity"]["result"] == "too_dry"


def test_too_humid_with_a_target_the_dht11_can_measure():
    kb = copy.deepcopy(KB)                    # no real crop has a target max <= 90
    kb["storage"]["tomato"].update(rh_min_pct=60, rh_max_pct=70)
    assert check("tomato", 13.0, 80.0, kb)["humidity"]["result"] == "too_humid"
    assert check("tomato", 13.0, 95.0, kb)["humidity"]["result"] == "too_humid"  # no cap
    assert check("tomato", 13.0, 65.0, kb)["humidity"]["result"] == "within"


def test_no_reading():
    s = storage.check("tomato", None, None, None, KB)
    assert s["state"] == "no_reading" and s["no_reading_text"] == "No recent reading"
    assert s["temp"] is None and s["note"] == storage.SENSOR_NOTE


def test_crop_unknown_shows_the_reading_only():
    s = check(None, 28.6, 57.0)
    assert s["state"] == "crop_unknown" and s["reading"] == "28.6 °C · 57 % RH"
    assert s["temp"] is None and s["sources"] == []


def test_sources_listed_with_their_verified_flag():
    s = check("tomato", 28.0, 57.0)
    assert [(src["name"].split(" - ")[0].split(",")[0], src["verified"]) for src in s["sources"]] \
        == [("UC Davis Postharvest Technology Center", True),
            ("FAO Agricultural Services Bulletin 152", True), ("Amha", True)]
    kb = copy.deepcopy(KB)                    # an unverified source is still tagged on the card
    kb["storage"]["tomato"]["verified"] = 0
    assert not check("tomato", 28.0, 57.0, kb)["sources"][0]["verified"]


def test_note():
    assert storage.SENSOR_NOTE == "Measured at the robot with a DHT11 (±2 °C, ±5 % RH)."


# ---------------------------------------------------------------- reading at scan time

def test_climate_columns_fresh_stale_unavailable():
    assert storage.climate_columns(FRESH) == (28.6, 57.0, 4.0)
    assert storage.climate_columns(dict(FRESH, age_s=30.0)) == (28.6, 57.0, 30.0)
    for bad in (dict(FRESH, age_s=31.0), dict(FRESH, available=False), dict(FRESH, temp_c=None),
                dict(FRESH, age_s=None), None, "x", {}):
        assert storage.climate_columns(bad) == (None, None, None)


def test_reading_text():
    assert storage.reading_text(28.6, 57.0) == "28.6 °C · 57 % RH"
    assert storage.reading_text(24.0, 91.0) == "24.0 °C · ≥ 90 % RH"
    assert storage.reading_text(None, None) == ""


def scan_with(scout_db, tmp_path, climate, plant="T-01", classifier=None):
    classifier = classifier or FixedClassifier([("tomato_fresh", 0.9), ("tomato_spoiled", 0.1)])
    return scan_plant(classifier, scout_db, image_bytes=photo(plant), now=NOW,
                      scans_dir=str(tmp_path / "scans"), climate=climate)


def stored(scout_db, scan_id):
    conn = db.open_db(scout_db)
    try:
        row = db.get_scan(conn, scan_id)
    finally:
        conn.close()
    return row["climate_temp_c"], row["climate_humidity_pct"], row["climate_age_s"]


def test_fresh_climate_is_stored_with_the_scan(scout_db, tmp_path):
    result = scan_with(scout_db, tmp_path, FRESH)
    assert result["climate"] == {"temp_c": 28.6, "humidity_pct": 57.0, "age_s": 4.0}
    assert stored(scout_db, result["scan_id"]) == (28.6, 57.0, 4.0)


def test_stale_or_missing_climate_stores_null(scout_db, tmp_path):
    for climate in (dict(FRESH, age_s=45.0), None, dict(FRESH, available=False)):
        result = scan_with(scout_db, tmp_path, climate)
        assert result["climate"] is None
        assert stored(scout_db, result["scan_id"]) == (None, None, None)


@pytest.mark.parametrize("preds, status", [
    ([("tomato_fresh", 0.9)], "healthy"), ([("tomato_spoiled", 0.9)], "action"),
    ([("tomato_spoiled", 0.52)], "uncertain"), ([("background", 0.9)], "no_produce")])
def test_climate_never_changes_the_status(scout_db, tmp_path, preds, status):
    hot = dict(FRESH, temp_c=35.0, humidity_pct=20.0)
    with_climate = scan_with(scout_db, tmp_path, hot, classifier=FixedClassifier(preds))
    without = scan_with(scout_db, tmp_path, None, classifier=FixedClassifier(preds))
    assert with_climate["status"] == without["status"] == status


def test_v3_database_migrates_to_v4_and_keeps_rows(tmp_path):
    path = str(tmp_path / "v3.db")
    conn = sqlite3.connect(path)
    for version, sql in enumerate(db.MIGRATIONS[:3], start=1):
        conn.executescript("BEGIN;\n%s\nPRAGMA user_version = %d;\nCOMMIT;" % (sql, version))
    for ts in ("2026-09-27T10:00:00", "2026-09-27T11:00:00"):
        conn.execute("INSERT INTO scans (timestamp, plant_id, image_path, top1_condition_id, "
                     "confidence, top3_json, model_name, status, notes, brightness) VALUES "
                     "(?, 'T-01', 'scans/a.jpg', 'tomato_fresh', 0.9, "
                     "'[[\"tomato_fresh\", 0.9]]', 'stub', 'healthy', 'n', 120)", (ts,))
    conn.execute("DELETE FROM scans WHERE id = 1")
    conn.commit()
    conn.close()

    conn = db.open_db(path)
    assert db.schema_version(conn) == 4
    [old] = db.list_scans(conn)
    assert (old["id"], old["timestamp"], old["notes"], old["brightness"]) == (
        2, "2026-09-27T11:00:00", "n", 120)
    assert (old["climate_temp_c"], old["climate_humidity_pct"], old["climate_age_s"]) == (
        None, None, None)
    new_id = db.insert_scan(conn, "2026-09-27T12:00:00", "T-01", "scans/b.jpg", None, None, [],
                            "stub", "healthy", climate=(24.0, 60.0, 2.0))
    assert new_id == 3 and db.get_scan(conn, 3)["climate_temp_c"] == 24.0


def test_fetch_climate_never_raises(monkeypatch):
    assert fetch_climate("127.0.0.1:1", timeout=0.2) is None   # nothing listens on port 1
    assert config.status_url_for("192.168.50.1:5000") == "http://192.168.50.1:5000/api/status"
    assert config.status_url_for("http://10.0.0.5/") == "http://10.0.0.5:5000/api/status"
    assert config.status_url_for("192.168.50.1") == "http://192.168.50.1:5000/api/status"
    assert config.status_url_for("127.0.0.1:5097") == "http://127.0.0.1:5097/api/status"


# ---------------------------------------------------------------- card + report

def card_for(scout_db, scan_id):
    conn = db.open_db(scout_db)
    try:
        return card.card_data(conn, scan_id)
    finally:
        conn.close()


def test_card_storage_section(scout_db, tmp_path):
    data = card_for(scout_db, scan_with(scout_db, tmp_path, FRESH)["scan_id"])
    html = card.render_card(data, "app")
    assert "<h3>Storage conditions</h3>" in html
    assert "28.6 °C · 57 % RH" in html and "measured 4 s before the scan" in html
    assert "Too warm" in html and "Too dry" in html
    assert "Spoils about 2.5-4.5x faster than at 15 °C" in html   # 28.6 °C: 2^1.36, 3^1.36
    assert "Source: UC Davis Postharvest Technology Center - Bell Pepper" not in html  # tomato
    assert "Source: UC Davis Postharvest Technology Center - Tomato" in html
    assert "Source: Amha, B. et al. (2025)" in html and "(to be verified)" not in html
    assert "Measured at the robot with a DHT11 (±2 °C, ±5 % RH)." in html
    assert "zero-energy cool chamber" in html


def test_card_without_reading_says_so(scout_db, tmp_path):
    html = card.render_card(card_for(scout_db, scan_with(scout_db, tmp_path, None)["scan_id"]))
    assert "<h3>Storage conditions</h3>" in html and "No recent reading" in html


def test_card_diagnosis_identical_with_and_without_climate(scout_db, tmp_path):
    a = card_for(scout_db, scan_with(scout_db, tmp_path, FRESH)["scan_id"])
    b = card_for(scout_db, scan_with(scout_db, tmp_path, None)["scan_id"])
    for key in ("status", "status_text", "title", "advice", "source", "percent", "top3"):
        assert a[key] == b[key], key


@pytest.mark.parametrize("preds", [[("tomato_spoiled", 0.52)], [("background", 0.9)]])
def test_no_storage_section_on_unsure_or_no_produce_cards(scout_db, tmp_path, preds):
    data = card_for(scout_db, scan_with(scout_db, tmp_path, FRESH,
                                        classifier=FixedClassifier(preds))["scan_id"])
    assert data["storage"] is None
    assert "Storage conditions" not in card.render_card(data)


def test_report_shows_climate_per_scan(scout_db, tmp_path):
    scan_with(scout_db, tmp_path, FRESH, plant="T-01")
    scan_with(scout_db, tmp_path, None, plant="P-01",
              classifier=FixedClassifier([("pepper_fresh", 0.9)]))
    html = report.render_report(scout_db)
    assert html.count("Climate at scan: ") == 1
    assert "Climate at scan: 28.6 °C · 57 % RH" in html
