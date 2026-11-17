"""SQLite storage for scout/: plants, conditions (the knowledge base) and scans.

- The schema is created by code. Each entry in MIGRATIONS is one version; the database remembers
  its version in `PRAGMA user_version`, so only the missing steps run (0 -> 1 -> 2 ...).
  To change the schema later: append a new SQL string, never edit an old one.
- The JSON seed files are the source of truth for plants and conditions: setup() upserts them at
  every start, so editing the JSON and restarting is enough.
- sqlite3 connections must not be shared between threads, so every thread (GUI worker, Flask
  request) opens its own with open_db(). Opening a local SQLite file is fast.
"""

import json
import os
import sqlite3
from typing import Dict, List, Optional, Tuple

from scout import config, storage

CROPS = ("tomato", "pepper")
CROP_NAMES = {"tomato": "Tomato", "pepper": "Green pepper"}  # how a crop id is shown on screen
CONDITION_TYPES = ("disease", "pest", "deficiency", "healthy")
SEVERITIES = ("low", "medium", "high")
STATUSES = ("healthy", "watch", "action", "uncertain", "no_produce")
UNKNOWN_PLANT = "unknown"  # plant_id of a scan where no QR stake was read

MIGRATIONS = [
    # v1
    """
    CREATE TABLE plants (
        id    TEXT PRIMARY KEY,                          -- "M-01", same text as in the QR code
        crop  TEXT CHECK (crop IN ('maize', 'tomato')),  -- NULL: seen on a QR, not in plants.json
        row   TEXT,
        label TEXT,
        notes TEXT
    );
    CREATE TABLE conditions (
        id               TEXT PRIMARY KEY,               -- "maize_common_rust"
        crop             TEXT NOT NULL CHECK (crop IN ('maize', 'tomato')),
        name             TEXT NOT NULL,
        type             TEXT NOT NULL CHECK (type IN ('disease', 'pest', 'deficiency', 'healthy')),
        symptoms         TEXT NOT NULL,
        what_to_do_now   TEXT NOT NULL,
        prevention       TEXT NOT NULL,
        when_to_get_help TEXT NOT NULL,
        severity         TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
        source_name      TEXT NOT NULL,
        source_url       TEXT NOT NULL,
        verified         INTEGER NOT NULL CHECK (verified IN (0, 1))
    );
    -- plant_id and top1_condition_id are not foreign keys on purpose: a scan must still be saved
    -- when no QR was read ("unknown") or when a model label is missing from the knowledge base.
    CREATE TABLE scans (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp         TEXT NOT NULL,                 -- local time, "2026-09-27T14:30:12"
        plant_id          TEXT NOT NULL,
        image_path        TEXT NOT NULL,                 -- relative to data/, "scans/....jpg"
        top1_condition_id TEXT,
        confidence        REAL,
        top3_json         TEXT NOT NULL,                 -- [["maize_common_rust", 0.87], ...]
        model_name        TEXT NOT NULL,
        status            TEXT NOT NULL CHECK (status IN ('healthy', 'watch', 'action', 'uncertain')),
        notes             TEXT,
        brightness        REAL                           -- mean HSV V of the photo, 0-255
    );
    CREATE INDEX scans_by_plant ON scans (plant_id, timestamp);
    """,
    # v2 (Phase 5c): crops are tomato + green pepper, maize is gone. SQLite can't change a CHECK,
    # so each table is rebuilt. Old maize plants stay (their scans still point at them) but lose
    # their crop, like a QR stake that isn't in plants.json. conditions is filled from the seed
    # at every start, so it is simply recreated empty. scans has no crop column: untouched.
    """
    CREATE TABLE plants_v2 (
        id    TEXT PRIMARY KEY,                           -- "T-01", same text as in the QR code
        crop  TEXT CHECK (crop IN ('tomato', 'pepper')),  -- NULL: not in plants.json
        row   TEXT,
        label TEXT,
        notes TEXT
    );
    INSERT INTO plants_v2 (id, crop, row, label, notes)
        SELECT id, CASE WHEN crop = 'maize' THEN NULL ELSE crop END, row, label, notes
        FROM plants;
    DROP TABLE plants;
    ALTER TABLE plants_v2 RENAME TO plants;
    DROP TABLE conditions;
    CREATE TABLE conditions (
        id               TEXT PRIMARY KEY,               -- "tomato_spoiled"
        crop             TEXT NOT NULL CHECK (crop IN ('tomato', 'pepper')),
        name             TEXT NOT NULL,
        type             TEXT NOT NULL CHECK (type IN ('disease', 'pest', 'deficiency', 'healthy')),
        symptoms         TEXT NOT NULL,
        what_to_do_now   TEXT NOT NULL,
        prevention       TEXT NOT NULL,
        when_to_get_help TEXT NOT NULL,
        severity         TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
        source_name      TEXT NOT NULL,
        source_url       TEXT NOT NULL,
        verified         INTEGER NOT NULL CHECK (verified IN (0, 1))
    );
    """,
    # v3 (photo retake): new status 'no_produce' (the model saw no tomato/pepper, class
    # "background"). SQLite can't change a CHECK, so scans is rebuilt; every row keeps its id.
    """
    CREATE TABLE scans_v3 (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp         TEXT NOT NULL,
        plant_id          TEXT NOT NULL,
        image_path        TEXT NOT NULL,
        top1_condition_id TEXT,
        confidence        REAL,
        top3_json         TEXT NOT NULL,
        model_name        TEXT NOT NULL,
        status            TEXT NOT NULL CHECK (status IN ('healthy', 'watch', 'action', 'uncertain',
                                                          'no_produce')),
        notes             TEXT,
        brightness        REAL
    );
    INSERT INTO scans_v3 (id, timestamp, plant_id, image_path, top1_condition_id, confidence,
                          top3_json, model_name, status, notes, brightness)
        SELECT id, timestamp, plant_id, image_path, top1_condition_id, confidence,
               top3_json, model_name, status, notes, brightness
        FROM scans;
    DROP TABLE scans;
    ALTER TABLE scans_v3 RENAME TO scans;
    CREATE INDEX scans_by_plant ON scans (plant_id, timestamp);
    """,
    # v4 (DHT11 phase 3): the robot's climate reading at scan time. NULL = no reading at most
    # 30 s old (old rows too). No CHECK changes, so no rebuild like v3: SQLite adds nullable
    # columns in place and every row keeps its data and id.
    """
    ALTER TABLE scans ADD COLUMN climate_temp_c REAL;         -- °C
    ALTER TABLE scans ADD COLUMN climate_humidity_pct REAL;   -- % RH (DHT11: ~90 max)
    ALTER TABLE scans ADD COLUMN climate_age_s REAL;          -- reading's age at scan time
    """,
]

PLANT_KEYS = {"id", "crop", "row", "label", "notes"}
CONDITION_KEYS = {"id", "crop", "name", "type", "symptoms", "what_to_do_now", "prevention",
                  "when_to_get_help", "severity", "source_name", "source_url", "verified"}


class SeedError(ValueError):
    """A seed file has a missing/unknown key or a bad value."""


# ---------------------------------------------------------------- connection + schema

def open_db(db_path: str = config.DB_PATH) -> sqlite3.Connection:
    """Open (and create/upgrade if needed) the database. Rows come back as sqlite3.Row."""
    folder = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    """Run every migration newer than the database's version, each one all-or-nothing."""
    for version in range(schema_version(conn) + 1, len(MIGRATIONS) + 1):
        sql = MIGRATIONS[version - 1]
        try:
            conn.executescript("BEGIN;\n%s\nPRAGMA user_version = %d;\nCOMMIT;" % (sql, version))
        except sqlite3.Error:
            conn.rollback()
            raise


def setup(db_path: str = config.DB_PATH, conditions_path: str = config.CONDITIONS_SEED,
          plants_path: str = config.PLANTS_FILE) -> None:
    """Called once at program start: create/upgrade the DB and load both seed files.
    The storage ranges in the conditions seed are checked too (read again by each card), so a
    typo there shows at start like one in a condition."""
    conn = open_db(db_path)
    try:
        load_conditions(conn, conditions_path)
        load_plants(conn, plants_path)
    finally:
        conn.close()
    storage.load_kb(conditions_path)


# ---------------------------------------------------------------- seed files

def read_seed(path: str, list_key: str, keys: set) -> List[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get(list_key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SeedError('%s: expected {"%s": [...]}' % (path, list_key))
    for i, item in enumerate(items):
        name = item.get("id", "item %d" % i) if isinstance(item, dict) else "item %d" % i
        if not isinstance(item, dict):
            raise SeedError("%s: %s is not an object" % (path, name))
        missing, unknown = keys - set(item), set(item) - keys
        if missing or unknown:
            raise SeedError("%s: %s: missing keys %s, unknown keys %s"
                            % (path, name, sorted(missing), sorted(unknown)))
    return items


def _upsert(conn: sqlite3.Connection, table: str, items: List[dict], keys: set,
            path: str, prune: bool = False) -> None:
    """Insert or update every item. prune=True also deletes rows whose id is not in items."""
    columns = sorted(keys)
    sql = "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(id) DO UPDATE SET %s" % (
        table, ", ".join(columns), ", ".join("?" * len(columns)),
        ", ".join("%s = excluded.%s" % (c, c) for c in columns if c != "id"))
    with conn:  # one transaction: a bad item leaves the table as it was
        if prune:
            ids = [item["id"] for item in items]
            conn.execute("DELETE FROM %s WHERE id NOT IN (%s)" % (table, ", ".join("?" * len(ids))),
                         ids)
        for item in items:
            try:
                conn.execute(sql, [item[c] for c in columns])
            except sqlite3.IntegrityError as err:  # a CHECK failed, e.g. severity "hihg"
                raise SeedError("%s: %s: %s" % (path, item["id"], err)) from err


def load_conditions(conn: sqlite3.Connection, path: str = config.CONDITIONS_SEED) -> int:
    """The seed is the whole knowledge base: a condition removed from it is removed here too
    (scans keep their condition id as text, so old scans are not lost)."""
    items = read_seed(path, "conditions", CONDITION_KEYS)
    _upsert(conn, "conditions", items, CONDITION_KEYS, path, prune=True)
    return len(items)


def load_plants(conn: sqlite3.Connection, path: str = config.PLANTS_FILE) -> int:
    items = read_seed(path, "plants", PLANT_KEYS)
    _upsert(conn, "plants", items, PLANT_KEYS, path)
    return len(items)


# ---------------------------------------------------------------- lookups

def _one(conn: sqlite3.Connection, sql: str, args: tuple) -> Optional[dict]:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def get_condition(conn: sqlite3.Connection, condition_id: str) -> Optional[dict]:
    return _one(conn, "SELECT * FROM conditions WHERE id = ?", (condition_id,))


def condition_ids(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT id FROM conditions")}


def get_plant(conn: sqlite3.Connection, plant_id: str) -> Optional[dict]:
    return _one(conn, "SELECT * FROM plants WHERE id = ?", (plant_id,))


def list_plants(conn: sqlite3.Connection) -> List[dict]:
    """Plants from plants.json first (by id), then ones only seen on a QR code (crop unknown)."""
    rows = conn.execute("SELECT * FROM plants ORDER BY crop IS NULL, row, id").fetchall()
    return [dict(r) for r in rows]


def add_unknown_plant(conn: sqlite3.Connection, plant_id: str) -> None:
    """A QR stake that isn't in plants.json: remember it so the report lists it."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO plants (id, crop, row, label, notes) "
                     "VALUES (?, NULL, NULL, 'added by scan', 'not in plants.json')", (plant_id,))


# ---------------------------------------------------------------- scans

def _scan_dict(row: sqlite3.Row) -> dict:
    scan = dict(row)
    scan["top3"] = json.loads(scan.pop("top3_json"))
    return scan


def insert_scan(conn: sqlite3.Connection, timestamp: str, plant_id: str, image_path: str,
                top1_condition_id: Optional[str], confidence: Optional[float], top3: list,
                model_name: str, status: str, brightness: Optional[float] = None,
                notes: Optional[str] = None,
                climate: Tuple[Optional[float], Optional[float], Optional[float]] = (None,) * 3
                ) -> int:
    """climate: (temp_c, humidity_pct, age_s) from storage.climate_columns, or all None."""
    temp_c, humidity_pct, age_s = climate
    with conn:
        cursor = conn.execute(
            "INSERT INTO scans (timestamp, plant_id, image_path, top1_condition_id, confidence, "
            "top3_json, model_name, status, notes, brightness, climate_temp_c, "
            "climate_humidity_pct, climate_age_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (timestamp, plant_id, image_path, top1_condition_id, confidence,
             json.dumps([[cid, conf] for cid, conf in top3]), model_name, status, notes,
             brightness, temp_c, humidity_pct, age_s))
    return cursor.lastrowid


def get_scan(conn: sqlite3.Connection, scan_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    return _scan_dict(row) if row else None


def list_scans(conn: sqlite3.Connection, plant_id: Optional[str] = None,
               limit: int = 50) -> List[dict]:
    """Newest first."""
    if plant_id is None:
        rows = conn.execute("SELECT * FROM scans ORDER BY timestamp DESC, id DESC LIMIT ?",
                            (limit,))
    else:
        rows = conn.execute("SELECT * FROM scans WHERE plant_id = ? "
                            "ORDER BY timestamp DESC, id DESC LIMIT ?", (plant_id, limit))
    return [_scan_dict(r) for r in rows.fetchall()]


def latest_scan_per_plant(conn: sqlite3.Connection) -> Dict[str, dict]:
    """{plant_id: newest scan} for every plant_id that has at least one scan."""
    rows = conn.execute(
        "SELECT * FROM scans s WHERE s.id = (SELECT s2.id FROM scans s2 "
        "WHERE s2.plant_id = s.plant_id ORDER BY s2.timestamp DESC, s2.id DESC LIMIT 1)")
    return {r["plant_id"]: _scan_dict(r) for r in rows.fetchall()}
