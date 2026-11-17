"""Phase 5a/5c: scout database (schema, migrations, seed files, lookups)."""

import json
import re
import sqlite3

import pytest

from scout import config, db
from scout.qr import PLANT_ID_RE

ADVICE_FIELDS = ("symptoms", "what_to_do_now", "prevention", "when_to_get_help")
SPOILED_SIGNS = ("soft", "sunken", "mould", "wrinkl")  # in both spoiled entries
# A number followed by a dose unit. (?![a-z]) = the unit ends there ("5 g" yes, "5 green" no).
# Storage conditions like "12.5-15 °C" or "90-95% relative humidity" are not doses: allowed.
DOSE_RE = re.compile(r"\d+(\.\d+)?\s*(ml|l|g|kg|mg|ppm|litres?|liters?|grams?)(?![a-z])"
                     r"|per\s+(litre|liter)", re.I)
CHEMICAL_RE = re.compile(r"fungicide|pesticide|insecticide|herbicide|chlorine|bleach|"
                         r"hypochlorite|spray", re.I)


def columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def test_new_database_gets_schema_v4(tmp_path):
    conn = db.open_db(str(tmp_path / "new.db"))
    assert db.schema_version(conn) == len(db.MIGRATIONS) == 4
    assert columns(conn, "plants") == {"id", "crop", "row", "label", "notes"}
    assert columns(conn, "conditions") == db.CONDITION_KEYS
    assert columns(conn, "scans") == {
        "id", "timestamp", "plant_id", "image_path", "top1_condition_id", "confidence",
        "top3_json", "model_name", "status", "notes", "brightness",
        "climate_temp_c", "climate_humidity_pct", "climate_age_s"}                # v4


def test_migrate_twice_changes_nothing(tmp_path):
    path = str(tmp_path / "a.db")
    db.open_db(path).close()
    conn = db.open_db(path)  # opening again must not re-run CREATE TABLE (would raise)
    assert db.schema_version(conn) == 4


def test_crops_are_tomato_and_pepper(scout_db):
    conn = db.open_db(scout_db)
    assert db.CROPS == ("tomato", "pepper")
    assert db.CROP_NAMES == {"tomato": "Tomato", "pepper": "Green pepper"}
    with pytest.raises(sqlite3.IntegrityError):  # maize is gone from the schema too
        with conn:
            conn.execute("INSERT INTO plants (id, crop) VALUES ('M-09', 'maize')")


def v1_database_with_maize(path):
    """A database as Phase 5a left it: schema v1, maize + tomato plants, a maize scan."""
    conn = sqlite3.connect(path)
    conn.executescript("BEGIN;\n%s\nPRAGMA user_version = 1;\nCOMMIT;" % db.MIGRATIONS[0])
    conn.execute("INSERT INTO plants VALUES ('M-02', 'maize', 'A', 'Maize 2', '')")
    conn.execute("INSERT INTO plants VALUES ('T-01', 'tomato', 'B', 'Tomato 1', '')")
    conn.execute("INSERT INTO conditions VALUES ('maize_common_rust', 'maize', 'Maize: common "
                 "rust', 'disease', 's', 'd', 'p', 'h', 'low', 'TO FILL', '', 0)")
    conn.execute("INSERT INTO scans (timestamp, plant_id, image_path, top1_condition_id, "
                 "confidence, top3_json, model_name, status, brightness) VALUES ('2026-09-27T"
                 "14:30:12', 'M-02', 'scans/old.jpg', 'maize_common_rust', 0.87, "
                 "'[[\"maize_common_rust\", 0.87]]', 'stub (scripted demo)', 'watch', 140)")
    conn.commit()
    conn.close()


def test_v1_database_with_maize_migrates_and_keeps_scans(tmp_path):
    path = str(tmp_path / "old.db")
    v1_database_with_maize(path)
    db.setup(path)
    conn = db.open_db(path)
    assert db.schema_version(conn) == 4
    old = db.get_plant(conn, "M-02")
    assert old["crop"] is None and old["label"] == "Maize 2"   # kept, but no crop any more
    assert db.get_plant(conn, "T-01")["crop"] == "tomato"
    assert db.get_condition(conn, "maize_common_rust") is None
    assert db.condition_ids(conn) == {"tomato_fresh", "tomato_spoiled",
                                      "pepper_fresh", "pepper_spoiled"}
    scans = db.list_scans(conn)
    assert len(scans) == 1 and scans[0]["top1_condition_id"] == "maize_common_rust"


def test_seed_is_exactly_the_four_produce_classes(scout_db):
    conn = db.open_db(scout_db)
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM conditions")}
    assert set(rows) == {"tomato_fresh", "tomato_spoiled", "pepper_fresh", "pepper_spoiled"}
    for cid, r in rows.items():
        assert r["crop"] == cid.split("_")[0]
        assert r["verified"] == 1                                # sourced advice
        for field in ADVICE_FIELDS:
            assert r[field].strip(), (cid, field)
            assert not r[field].startswith("VERIFY"), (cid, field)  # no placeholder left
        if cid.endswith("_fresh"):
            assert (r["type"], r["severity"]) == ("healthy", "low")
        else:
            assert (r["type"], r["severity"]) == ("disease", "high")
            assert "Remove from batch" in r["what_to_do_now"]
            assert "do not sell" in r["what_to_do_now"]
            for sign in SPOILED_SIGNS:
                assert sign in r["symptoms"].lower(), (cid, sign)
    assert rows["pepper_spoiled"]["name"] == "Spoiled green pepper"


def test_seed_has_no_doses_or_chemicals():
    with open(config.CONDITIONS_SEED, encoding="utf-8") as f:
        text = f.read()
    assert not DOSE_RE.search(text), DOSE_RE.search(text)
    assert not CHEMICAL_RE.search(text), CHEMICAL_RE.search(text)


@pytest.mark.parametrize("text, is_dose", [
    ("mix 20 ml in water", True), ("use 5 g per crate", True), ("2 litres", True),
    ("50 ppm", True), ("per litre of water", True),
    ("keep at 12.5-15 °C", False), ("90-95% relative humidity", False),
    ("for 3-5 weeks", False), ("5 green peppers", False),
])
def test_dose_check_itself(text, is_dose):
    assert bool(DOSE_RE.search(text)) == is_dose


def test_verified_advice_names_a_real_source():
    with open(config.CONDITIONS_SEED, encoding="utf-8") as f:
        conditions = json.load(f)["conditions"]
    for c in conditions:
        if c["verified"]:
            assert c["source_name"].strip() and c["source_name"] != "TO FILL", c["id"]
            assert c["source_url"].startswith("https://"), c["id"]
            assert " " not in c["source_url"], c["id"]
        else:  # unverified: never an invented source
            assert c["source_url"] == "" and c["source_name"] == "TO FILL", c["id"]


def test_plants_file_is_the_demo_batches(scout_db):
    conn = db.open_db(scout_db)
    plants = db.list_plants(conn)
    assert [p["id"] for p in plants] == ["T-01", "T-02", "T-03", "T-04",   # row A first
                                         "P-01", "P-02", "P-03", "P-04"]
    for p in plants:
        assert PLANT_ID_RE.match(p["id"])
        expected = ("tomato", "A") if p["id"].startswith("T-") else ("pepper", "B")
        assert (p["crop"], p["row"]) == expected
    assert db.get_plant(conn, "T-04")["label"] == "spare"
    assert db.get_plant(conn, "P-04")["label"] == "spare"


def write_seed(path, conditions):
    path.write_text(json.dumps({"conditions": conditions}), encoding="utf-8")


def base_condition(**changes):
    c = {"id": "pepper_x", "crop": "pepper", "name": "X", "type": "disease", "symptoms": "s",
         "what_to_do_now": "d", "prevention": "p", "when_to_get_help": "h", "severity": "low",
         "source_name": "n", "source_url": "", "verified": 0}
    c.update(changes)
    return c


def test_editing_the_seed_updates_the_database(tmp_path):
    conn = db.open_db(str(tmp_path / "a.db"))
    seed = tmp_path / "seed.json"
    write_seed(seed, [base_condition()])
    db.load_conditions(conn, str(seed))
    write_seed(seed, [base_condition(severity="high", verified=1)])
    db.load_conditions(conn, str(seed))
    row = db.get_condition(conn, "pepper_x")
    assert (row["severity"], row["verified"]) == ("high", 1)
    assert conn.execute("SELECT COUNT(*) FROM conditions").fetchone()[0] == 1


def test_condition_removed_from_seed_is_removed_from_database(tmp_path):
    conn = db.open_db(str(tmp_path / "a.db"))
    seed = tmp_path / "seed.json"
    write_seed(seed, [base_condition(), base_condition(id="pepper_y")])
    db.load_conditions(conn, str(seed))
    write_seed(seed, [base_condition(id="pepper_y")])
    db.load_conditions(conn, str(seed))
    assert db.condition_ids(conn) == {"pepper_y"}


def test_seed_with_typo_key_gives_clear_error(tmp_path):
    conn = db.open_db(str(tmp_path / "a.db"))
    seed = tmp_path / "seed.json"
    bad = base_condition()
    bad["severty"] = bad.pop("severity")
    write_seed(seed, [bad])
    with pytest.raises(db.SeedError, match="pepper_x.*missing keys \\['severity'\\].*severty"):
        db.load_conditions(conn, str(seed))


def test_seed_with_bad_value_gives_clear_error_and_loads_nothing(tmp_path):
    conn = db.open_db(str(tmp_path / "a.db"))
    seed = tmp_path / "seed.json"
    write_seed(seed, [base_condition(id="ok_one")])
    db.load_conditions(conn, str(seed))
    write_seed(seed, [base_condition(id="ok_two"), base_condition(crop="maize")])
    with pytest.raises(db.SeedError, match="pepper_x"):
        db.load_conditions(conn, str(seed))
    assert db.condition_ids(conn) == {"ok_one"}  # nothing added, nothing pruned


def test_lookups_hit_and_miss(scout_db):
    conn = db.open_db(scout_db)
    assert db.get_condition(conn, "pepper_spoiled")["crop"] == "pepper"
    assert db.get_condition(conn, "nope") is None
    assert db.get_plant(conn, "P-02")["crop"] == "pepper"
    assert db.get_plant(conn, "Z-99") is None


def test_unknown_plant_is_added_once_and_listed_last(scout_db):
    conn = db.open_db(scout_db)
    db.add_unknown_plant(conn, "A-01")
    db.add_unknown_plant(conn, "A-01")
    plants = db.list_plants(conn)
    assert plants[-1]["id"] == "A-01" and plants[-1]["crop"] is None
    assert len(plants) == 9


def add_scan(conn, ts, plant_id, status="healthy"):
    return db.insert_scan(conn, ts, plant_id, "scans/x.jpg", "tomato_fresh", 0.9,
                          [("tomato_fresh", 0.9)], "stub", status)


def test_scans_newest_first_and_latest_per_plant(scout_db):
    conn = db.open_db(scout_db)
    add_scan(conn, "2026-09-27T10:00:00", "T-01")
    newest = add_scan(conn, "2026-09-27T11:00:00", "T-01", "action")
    add_scan(conn, "2026-09-27T10:30:00", "P-01")
    scans = db.list_scans(conn)
    assert [s["timestamp"][11:13] for s in scans] == ["11", "10", "10"]
    assert scans[0]["top3"] == [["tomato_fresh", 0.9]]
    assert len(db.list_scans(conn, "T-01")) == 2
    latest = db.latest_scan_per_plant(conn)
    assert set(latest) == {"T-01", "P-01"}
    assert latest["T-01"]["id"] == newest and latest["T-01"]["status"] == "action"


def test_bad_status_is_refused(scout_db):
    conn = db.open_db(scout_db)
    with pytest.raises(sqlite3.IntegrityError):
        add_scan(conn, "2026-09-27T10:00:00", "T-01", status="fine")


def test_every_status_is_accepted(scout_db):
    conn = db.open_db(scout_db)
    assert db.STATUSES == ("healthy", "watch", "action", "uncertain", "no_produce")
    for status in db.STATUSES:
        add_scan(conn, "2026-09-27T10:00:00", "T-01", status)
    assert len(db.list_scans(conn)) == len(db.STATUSES)


def test_v2_database_migrates_to_v4_and_keeps_scans(tmp_path):
    path = str(tmp_path / "v2.db")
    conn = sqlite3.connect(path)
    for version, sql in enumerate(db.MIGRATIONS[:2], start=1):
        conn.executescript("BEGIN;\n%s\nPRAGMA user_version = %d;\nCOMMIT;" % (sql, version))
    with pytest.raises(sqlite3.IntegrityError):  # v2 did not know no_produce
        with conn:
            conn.execute("INSERT INTO scans (timestamp, plant_id, image_path, top3_json, "
                         "model_name, status) VALUES ('t', 'T-01', 'x', '[]', 'm', 'no_produce')")
    for ts in ("2026-09-27T10:00:00", "2026-09-27T11:00:00"):
        conn.execute("INSERT INTO scans (timestamp, plant_id, image_path, top1_condition_id, "
                     "confidence, top3_json, model_name, status, notes, brightness) VALUES "
                     "(?, 'T-01', 'scans/a.jpg', 'tomato_fresh', 0.9, "
                     "'[[\"tomato_fresh\", 0.9]]', 'stub', 'healthy', 'n', 120)", (ts,))
    conn.execute("DELETE FROM scans WHERE id = 1")  # ids must survive, gaps included
    conn.commit()
    conn.close()

    conn = db.open_db(path)
    assert db.schema_version(conn) == 4
    scans = db.list_scans(conn)
    assert [(s["id"], s["timestamp"], s["notes"], s["brightness"]) for s in scans] == [
        (2, "2026-09-27T11:00:00", "n", 120)]
    new_id = add_scan(conn, "2026-09-27T12:00:00", "T-01", "no_produce")
    assert new_id == 3                                   # AUTOINCREMENT goes on after the old ids
    index = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                         "AND tbl_name = 'scans'").fetchall()
    assert [r[0] for r in index] == ["scans_by_plant"]
