"""Phase 5a/5c: the laptop scout server (port 5050), the field report and the card pages."""

import os
import re

import pytest

from scout import card, config, db, report
from scout.classifier import StubClassifier
from scout.server import create_app
from test_scout_pipeline import FixedClassifier, photo


@pytest.fixture
def env(scout_db, tmp_path):
    """Test client whose 'camera' returns whatever photo is in env['next']."""
    state = {"next": photo("T-02")}
    scans_dir = str(tmp_path / "scans")
    app = create_app(StubClassifier.from_file(config.STUB_SCRIPT), scout_db,
                     scans_dir=scans_dir, get_image=lambda: state["next"])
    state.update(client=app.test_client(), db=scout_db, scans_dir=scans_dir)
    return state


def test_post_scan(env):
    r = env["client"].post("/scan")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] and data["plant_id"] == "T-02" and data["status"] == "action"
    assert "image_file" not in data  # no laptop file paths in the API


@pytest.mark.parametrize("plant_id", ["T-03", "P-01"])
def test_post_scan_with_batch_override(env, plant_id):
    r = env["client"].post("/scan", json={"plant_id": plant_id})
    assert r.get_json()["plant_id"] == plant_id


@pytest.mark.parametrize("bad", ["../etc", "P01", 5])
def test_post_scan_rejects_bad_plant_id(env, bad):
    r = env["client"].post("/scan", json={"plant_id": bad})
    assert r.status_code == 400 and not r.get_json()["ok"]
    assert "T-01" in r.get_json()["error"]


def test_post_scan_without_camera_is_502(scout_db, tmp_path):
    # Nothing listens on port 9 (discard): the snapshot fails fast, like a robot that is off.
    app = create_app(StubClassifier(), scout_db, snapshot_url="http://127.0.0.1:9/?action=snapshot",
                     scans_dir=str(tmp_path / "scans"))
    r = app.test_client().post("/scan")
    assert r.status_code == 502
    assert "no camera snapshot" in r.get_json()["error"]


def test_get_scans_and_filter(env):
    c = env["client"]
    c.post("/scan")
    env["next"] = photo("P-01")
    c.post("/scan")
    all_scans = c.get("/scans").get_json()["scans"]
    assert {s["plant_id"] for s in all_scans} == {"T-02", "P-01"}
    only = c.get("/scans?plant_id=P-01").get_json()["scans"]
    assert [s["plant_id"] for s in only] == ["P-01"]
    assert len(c.get("/scans?limit=1").get_json()["scans"]) == 1
    assert c.get("/scans?limit=abc").status_code == 400


def test_get_plant(env):
    c = env["client"]
    c.post("/scan")
    data = c.get("/plants/T-02").get_json()
    assert data["plant"]["crop"] == "tomato" and len(data["scans"]) == 1
    assert c.get("/plants/P-01").get_json()["scans"] == []   # in plants.json, not scanned yet
    assert c.get("/plants/Z-99").status_code == 404


def test_scan_photo_is_served_and_paths_cannot_escape(env):
    c = env["client"]
    path = c.post("/scan").get_json()["image_path"]         # "scans/....jpg"
    r = c.get("/" + path)
    assert r.status_code == 200 and r.data == env["next"]
    assert c.get("/scans/../agriscout.db").status_code == 404
    assert c.get("/scans/x.txt").status_code == 404


# ---------------------------------------------------------------- field report

def batch_card(html: str, plant_id: str) -> str:
    """The report's <article> for one batch."""
    start = html.rindex("<article", 0, html.index('<span class="batch-id">%s</span>' % plant_id))
    return html[start:html.index("</article>", start)]


def tile_count(html: str, status: str) -> int:
    m = re.search(r'<div class="tile status-%s"><div class="count">(\d+)</div>' % status, html)
    return int(m.group(1)) if m else -1


def test_report_tiles_and_batch_cards(env):
    c = env["client"]
    scan_id = c.post("/scan").get_json()["scan_id"]          # T-02 spoiled -> Remove
    env["next"] = photo("P-01")
    c.post("/scan")                                          # P-01 fresh -> Good
    env["next"] = photo("T-03")
    c.post("/scan")                                          # T-03 52 % -> Not sure
    env["next"] = photo()
    c.post("/scan")                                          # no QR -> unknown, Not sure
    html = c.get("/report").get_data(as_text=True)

    ids = re.findall(r'<span class="batch-id">([^<]+)</span>', html)
    assert ids == ["T-01", "T-02", "T-03", "T-04", "P-01", "P-02", "P-03", "P-04", "unknown"]
    assert tile_count(html, "healthy") == 1
    assert tile_count(html, "action") == 1
    assert tile_count(html, "uncertain") == 2
    assert tile_count(html, "none") == 5
    assert tile_count(html, "watch") == -1                   # "Check" only when used
    assert ">Good<" in html and ">Remove<" in html and ">Not scanned<" in html

    t02 = batch_card(html, "T-02")
    assert "Spoiled tomato" in t02 and ">Remove<" in t02
    assert 'href="/cards/%d.html">View</a>' % scan_id in t02
    assert re.search(r'<img class="thumb" src="/scans/20\d{6}-\d{6}_T-02\.jpg"', t02)
    assert "Tomato · row A" in t02
    assert "no photo yet" in batch_card(html, "T-01") and "View" not in batch_card(html, "T-01")
    assert "Green pepper · row B" in batch_card(html, "P-01")
    assert "Stand-in demo" in html and "stub (scripted demo)" in html


def test_report_uncertain_card_hides_the_guess(env):
    env["next"] = photo("T-03")                              # stub: spoiled 52 % -> uncertain
    env["client"].post("/scan")
    html = env["client"].get("/report").get_data(as_text=True)
    t03 = batch_card(html, "T-03")
    assert "Not sure - scan again" in t03 and ">Not sure<" in t03
    assert "Spoiled tomato" not in html                      # never the guess as a diagnosis


def test_report_no_produce_tile_and_batch_card(scout_db, tmp_path):
    app = create_app(FixedClassifier([("background", 0.9), ("tomato_fresh", 0.1)]), scout_db,
                     scans_dir=str(tmp_path / "scans"), get_image=lambda: photo("T-01"))
    c = app.test_client()
    html = c.get("/report").get_data(as_text=True)
    assert tile_count(html, "no_produce") == -1              # only shown when used
    assert c.post("/scan").get_json()["status"] == "no_produce"
    html = c.get("/report").get_data(as_text=True)
    assert tile_count(html, "no_produce") == 1
    assert '<div class="what">No tomato or green pepper found</div>' in html     # the tile
    t01 = batch_card(html, "T-01")
    assert "status-no_produce" in t01 and ">No tomato/pepper<" in t01           # the chip
    assert '<div class="title">No tomato or green pepper found</div>' in t01
    assert "STUB" not in html                                # not the stub: no stub banner


def test_report_stub_banner(env):
    env["client"].post("/scan")
    html = env["client"].get("/report").get_data(as_text=True)
    assert card.STUB_BANNER in html


def test_report_is_offline(env):
    env["client"].post("/scan")
    html = env["client"].get("/report").get_data(as_text=True)
    assert "<script" not in html
    assert not re.search(r'(src|href)="https?://', html)     # no CDN, no web fonts


def test_report_escapes_database_text(env):
    env["client"].post("/scan")
    conn = db.open_db(env["db"])
    with conn:
        conn.execute("UPDATE plants SET label = '<script>alert(1)</script>' WHERE id = 'T-02'")
    html = env["client"].get("/report").get_data(as_text=True)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_report_file_writes_cards_with_relative_links(env, tmp_path):
    scan_id = env["client"].post("/scan").get_json()["scan_id"]
    path = report.write_report(env["db"], str(tmp_path / "report.html"))
    html = open(path, encoding="utf-8").read()
    assert re.search(r'src="scans/20\d{6}-\d{6}_T-02\.jpg"', html)
    assert 'href="cards/%d.html"' % scan_id in html
    card_html = open(tmp_path / "cards" / ("%d.html" % scan_id), encoding="utf-8").read()
    assert re.search(r'src="\.\./scans/20\d{6}-\d{6}_T-02\.jpg"', card_html)
    assert 'href="../report.html"' in card_html
    assert "Save / Share" not in card_html and "<script" not in card_html


def test_old_maize_scans_do_not_break_the_report(env):
    conn = db.open_db(env["db"])
    with conn:  # what a Phase 5a database looks like after the v2 migration
        conn.execute("INSERT INTO plants (id, crop, row, label) VALUES ('M-02', NULL, 'A', "
                     "'Maize 2')")
        conn.execute("INSERT INTO plants (id, crop, row, label) VALUES ('M-03', NULL, 'A', "
                     "'Maize 3')")                            # never scanned: left out
    scan_id = db.insert_scan(conn, "2026-09-27T14:30:12", "M-02", "scans/old.jpg",
                             "maize_common_rust", 0.87, [("maize_common_rust", 0.87)],
                             "stub (scripted demo)", "watch", 140)
    html = env["client"].get("/report").get_data(as_text=True)
    old = batch_card(html, "M-02")
    assert "maize_common_rust" in old and "Unknown crop" in old and ">Check<" in old
    assert '<span class="batch-id">M-03</span>' not in html
    assert tile_count(html, "none") == 8                     # only the 8 real batches
    r = env["client"].get("/cards/%d.html" % scan_id)
    assert r.status_code == 200 and "maize_common_rust" in r.get_data(as_text=True)


# ---------------------------------------------------------------- card pages

def test_card_page_is_served_in_report_mode(env):
    scan_id = env["client"].post("/scan").get_json()["scan_id"]
    html = env["client"].get("/cards/%d.html" % scan_id).get_data(as_text=True)
    assert "Spoiled tomato" in html and "88% match" in html
    assert 'href="/report"' in html                          # back arrow -> report
    assert re.search(r'src="/scans/20\d{6}-\d{6}_T-02\.jpg"', html)
    assert "Save / Share" not in html and "<script" not in html
    assert not re.search(r'(src|href)="https?://', html)


def test_card_page_404(env):
    assert env["client"].get("/cards/999.html").status_code == 404
    assert env["client"].get("/cards/abc.html").status_code == 404


def test_uncertain_unknown_label_card_shows_raw_id_as_guess(scout_db, tmp_path):
    app = create_app(FixedClassifier([("banana_wilt", 0.99)]), scout_db,
                     scans_dir=str(tmp_path / "scans"), get_image=lambda: photo("T-01"))
    client = app.test_client()
    scan_id = client.post("/scan").get_json()["scan_id"]
    html = client.get("/cards/%d.html" % scan_id).get_data(as_text=True)
    assert "Not sure - scan again" in html and "best guess: banana_wilt 99 %" in html
    assert card.UNCERTAIN_ADVICE in html and "Do now" not in html


def test_post_scan_stores_the_robot_climate(scout_db, tmp_path):
    climate = {"available": True, "temp_c": 27.0, "humidity_pct": 55.0, "age_s": 3.0}
    app = create_app(StubClassifier.from_file(config.STUB_SCRIPT), scout_db,
                     scans_dir=str(tmp_path / "scans"), get_image=lambda: photo("T-01"),
                     get_climate=lambda: climate)
    data = app.test_client().post("/scan").get_json()
    assert data["climate"] == {"temp_c": 27.0, "humidity_pct": 55.0, "age_s": 3.0}


def test_post_scan_without_climate(env):
    assert env["client"].post("/scan").get_json()["climate"] is None
