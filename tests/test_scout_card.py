"""Phase 5c: the scan result card (scout/card.py + templates/card.html)."""

import re

import pytest
from markupsafe import escape

from scout import card, config, db
from scout.classifier import StubClassifier
from scout.pipeline import scan_plant
from test_scout_pipeline import NOW, FixedClassifier, photo


@pytest.fixture
def scan(scout_db, tmp_path):
    """scan(plant_id) -> card_data of a real stub scan."""
    stub = StubClassifier.from_file(config.STUB_SCRIPT)

    def run(plant_id, classifier=stub):
        result = scan_plant(classifier, scout_db, image_bytes=photo(plant_id), now=NOW,
                            scans_dir=str(tmp_path / "scans"))
        conn = db.open_db(scout_db)
        try:
            return card.card_data(conn, result["scan_id"])
        finally:
            conn.close()
    return run


def header_status(html: str) -> str:
    return re.search(r'<main class="card status-(\w+)">', html).group(1)


def test_status_labels():
    assert card.STATUS_TEXT == {"healthy": "Good", "watch": "Check", "action": "Remove",
                                "uncertain": "Not sure - scan again",
                                "no_produce": "No tomato or green pepper found",
                                "none": "Not scanned"}
    assert set(card.STATUS_TEXT) == set(db.STATUSES) | {"none"}
    assert card.STATUS_SHORT["no_produce"] == "No tomato/pepper"


def test_good_card(scan):
    data = scan("P-01")                                      # fresh pepper 91 %
    html = card.render_card(data, "app")
    assert header_status(html) == "healthy"
    assert data["chip"] == "P-01 · row B" and data["crop_name"] == "Green pepper"
    assert data["when"] == "27 Sep 2026 · 14:30"
    assert "Fresh green pepper" in html and "91% match" in html
    assert "Top 3 possibilities" in html
    for title in ("Do now", "Symptoms", "Prevention", "Get help when"):
        assert "<h3>%s</h3>" % title in html
    # sourced advice: the footer names the source, no "not verified" tag
    assert data["advice_verified"] and "Advice not verified" not in html
    assert re.search(r"Light level %.0f/255 · Source: UC Davis Postharvest" % data["brightness"],
                     html)
    assert "Source to be verified" not in html


def test_unverified_advice_is_tagged(scan, scout_db):
    conn = db.open_db(scout_db)
    with conn:  # e.g. a new entry added to the seed before its source is checked
        conn.execute("UPDATE conditions SET verified = 0 WHERE id = 'pepper_fresh'")
    html = card.render_card(scan("P-01"))
    assert "Advice not verified" in html and "Source to be verified" in html


def test_remove_card(scan):
    data = scan("T-02")                                      # spoiled tomato 88 %
    html = card.render_card(data, "app")
    assert header_status(html) == "action" and data["status_text"] == "Remove"
    assert "Spoiled tomato" in html and "88% match" in html
    assert "Remove from batch" in html
    assert '<span class="dot dot-spoiled"></span><span class="name">Spoiled tomato</span>' in html
    assert '<span class="dot dot-fresh"></span><span class="name">Fresh tomato</span>' in html
    assert "ignored other-crop results: pepper_spoiled" in html


def test_check_card(scan, scout_db):
    # No seed entry gives "watch" any more, but the card must still show it: severity low.
    conn = db.open_db(scout_db)
    with conn:
        conn.execute("UPDATE conditions SET severity = 'low' WHERE id = 'tomato_spoiled'")
    data = scan("T-02")
    assert data["status"] == "watch"
    assert header_status(card.render_card(data)) == "watch" and data["status_text"] == "Check"


def test_uncertain_card_keeps_the_rule(scan, scout_db):
    data = scan("T-03")                                      # spoiled 52 % -> uncertain
    html = card.render_card(data, "app")
    assert header_status(html) == "uncertain"
    assert "<h2>Not sure - scan again</h2>" in html
    assert '<div class="guess">best guess: Spoiled tomato 52 %</div>' in html
    assert "match" not in html                               # no pill on a guess
    assert "Top 3 possibilities" in html                     # kept (decided in Phase 5c)
    assert card.UNCERTAIN_ADVICE in html
    spoiled = db.get_condition(db.open_db(scout_db), "tomato_spoiled")
    for key in ("what_to_do_now", "symptoms", "prevention", "when_to_get_help"):
        assert spoiled[key] not in html                      # none of the guess's advice
    assert "<h3>" not in html and "Advice not verified" not in html


def test_no_qr_card(scan):
    data = scan(None)
    assert data["chip"] == "No QR code" and data["crop_name"] == "Unknown crop"
    assert data["crop_icon"] == "crop_unknown" and data["uncertain"]


def test_no_qr_uncertain_card_shows_no_source(scan):
    # stub default: tomato_fresh 45 % -> uncertain, crop unknown: no tomato source in the footer
    data = scan(None)
    html = card.render_card(data)
    assert data["source"] == "" and "Source" not in html and "UC Davis" not in html
    assert "Model: stub (scripted demo)" in html


def test_no_qr_confident_card_shows_diagnosed_crop_advice_and_source(scan):
    data = scan(None, FixedClassifier([("tomato_spoiled", 0.8), ("background", 0.1)]))
    html = card.render_card(data)
    assert data["crop_name"] == "Tomato" and data["crop_icon"] == "tomato"
    assert data["status"] == "action" and "Do now" in html
    assert data["source"].startswith("Source: UC Davis")


def no_produce_scan(scan, plant_id="T-01", preds=None):
    preds = preds or [("background", 0.9), ("tomato_fresh", 0.06), ("tomato_spoiled", 0.04)]
    return scan(plant_id, FixedClassifier(preds))


@pytest.mark.parametrize("mode", ["app", "report", "export"])
def test_no_produce_card(scan, mode):
    data = no_produce_scan(scan)
    html = card.render_card(data, mode, back_href="/report")
    assert header_status(html) == "no_produce"
    assert data["status_text"] == "No tomato or green pepper found"
    assert "<h2>No tomato or green pepper found</h2>" in html
    assert card.NO_PRODUCE_TEXT == ("This doesn't look like a tomato or a green pepper. Place "
                                    "one in front of the camera and scan again.")
    # autoescape writes the apostrophe as &#39;: compare with the escaped text
    assert str(escape(card.NO_PRODUCE_TEXT)) in html
    assert card.UNCERTAIN_ADVICE not in html                                      # item 6
    assert not re.search(r"(?i)no produce", html)    # old wording (the class id no_produce stays)
    assert "%" not in re.sub(r"<style>.*?</style>", "", html, flags=re.S)       # no percentage
    assert 'class="bar"' not in html and "Top 3 possibilities" not in html
    assert "<h3>" not in html and "Do now" not in html                         # no advice
    assert data["source"] == "" and "Source" not in html
    assert data["crop_name"] == "Tomato"                     # the stake still says the crop
    assert "Model: fixed" in html                            # footer always names the model


def test_no_produce_without_qr_is_unknown_crop(scan):
    data = no_produce_scan(scan, None, [("background", 0.4), ("tomato_spoiled", 0.35)])
    assert data["status"] == "no_produce" and data["crop_name"] == "Unknown crop"


def test_uncertain_advice_is_still_the_retake_text(scan):
    data = scan("T-03")
    assert data["retake_advice"] == card.UNCERTAIN_ADVICE
    assert "turn the damaged side" in data["retake_advice"]


def test_background_in_top3_has_a_readable_name(scan):
    data = scan("T-01", FixedClassifier([("tomato_spoiled", 0.5), ("background", 0.45)]))
    assert data["status"] == "uncertain"
    assert data["top3"][1] == {"name": "No tomato or green pepper", "percent": 45,
                               "kind": "unknown"}


@pytest.mark.parametrize("mode", ["app", "report", "export"])
def test_stub_banner_on_every_stub_card(scan, mode):
    html = card.render_card(scan("T-01"), mode, back_href="/report")
    assert '<div class="stub-banner">STUB - not using the camera image</div>' in html
    assert "Model: stub (scripted demo)" in html


def test_no_stub_banner_for_a_real_model(scan):
    html = card.render_card(scan("T-01", FixedClassifier([("tomato_fresh", 0.9)])))
    assert "stub-banner\">" not in html and card.STUB_BANNER not in html
    assert "Model: fixed" in html


def test_demo_banner_wording(scan):
    stub = card.render_card(scan("T-01"))
    assert ("Stand-in demo - scripted, not a trained model. Confirm important decisions with the "
            "camp extension officer.") in stub
    assert "Prototype model" not in stub
    html = card.render_card(scan("T-01", FixedClassifier([("tomato_fresh", 0.9)])))
    assert ("Prototype model - trained on our own photos of tomatoes and green peppers. Confirm "
            "important decisions with the camp extension officer.") in html
    assert "Stand-in" not in html
    assert "agronomist" not in stub and "agronomist" not in html  # old wording gone


def test_modes(scan):
    data = scan("T-02")
    app = card.render_card(data, "app")
    assert 'href="%sback"' % card.ACTION_BASE in app
    assert 'href="%ssave"' % card.ACTION_BASE in app and 'href="%sscan"' % card.ACTION_BASE in app
    assert card.ACTION_BASE.startswith("https://agriscout.invalid/")  # can never reach a server
    report = card.render_card(data, "report", image_base="/", back_href="/report")
    assert 'href="/report"' in report and "Save / Share" not in report
    assert 'src="/scans/20260927-143012_T-02.jpg"' in report
    export = card.render_card(data, "export")
    assert 'class="back"' not in export and "Save / Share" not in export
    with pytest.raises(ValueError):
        card.render_card(data, "phone")


@pytest.mark.parametrize("mode", ["app", "report"])
def test_screen_card_has_the_wide_layouts(scan, mode):
    html = card.render_card(scan("T-02"), mode, back_href="/report")
    assert "@media (min-width: 1000px)" in html               # two columns, bigger type
    assert "grid-template-columns: 55fr 45fr" in html
    assert ".title-row h2 { font-size: 40px; }" in html
    assert "@media (min-width: 1400px)" in html               # 2x2 advice grid
    assert '<div class="advice-grid">' in html and html.count('<div class="advice">') == 4
    assert '<div class="left">' in html and '<div class="photo-wrap">' in html


def test_export_stays_phone_width(scan):
    html = card.render_card(scan("T-02"), "export")
    assert "@media" not in html                               # no wide rules at all
    assert ".card { max-width: 480px;" in html
    assert "head-buttons\">" not in html and "Save / Share" not in html


def test_app_buttons_in_header_and_at_bottom(scan):
    html = card.render_card(scan("T-02"), "app")
    assert '<div class="head-buttons">' in html               # shown on wide layouts
    assert '<div class="buttons bottom-buttons">' in html     # shown on narrow
    assert html.count('href="%ssave"' % card.ACTION_BASE) == 2
    assert html.count('href="%sscan"' % card.ACTION_BASE) == 2


@pytest.mark.parametrize("mode", ["app", "report", "export"])
def test_card_is_offline(scan, mode):
    html = card.render_card(scan("T-02"), mode, back_href="../report.html")
    assert "<script" not in html and "@import" not in html
    links = re.findall(r'(?:src|href)="(https?://[^"]*)"', html)
    assert all(link.startswith(card.ACTION_BASE) for link in links)  # only the app buttons


def test_card_escapes_database_text(scan, scout_db):
    data = scan("T-02")
    data["notes"] = "<img src=x onerror=alert(1)>"
    html = card.render_card(data)
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_old_maize_scan_card(scout_db):
    conn = db.open_db(scout_db)
    db.add_unknown_plant(conn, "M-02")
    scan_id = db.insert_scan(conn, "2026-09-27T14:30:12", "M-02", "scans/old.jpg",
                             "maize_common_rust", 0.87, [("maize_common_rust", 0.87)],
                             "stub (scripted demo)", "watch", 140)
    data = card.card_data(conn, scan_id)
    html = card.render_card(data)
    assert data["title"] == "maize_common_rust" and data["crop_name"] == "Unknown crop"
    assert data["advice"] == [] and "not in the knowledge base" in html
    assert card.card_data(conn, 999) is None


def test_export_name(scan):
    assert card.export_name(scan("T-02")) == "T-02_20260927-143012.png"
    assert card.export_name(scan(None)) == "unknown_20260927-143012.png"


@pytest.mark.parametrize("name, conf, expected", [
    ("Spoiled tomato", 0.52, "best guess: Spoiled tomato 52 %"),
    ("banana_wilt", 0.994, "best guess: banana_wilt 99 %"),
    ("X", None, "best guess: X"),
    (None, 0.4, ""),
    ("", 0.4, ""),
])
def test_best_guess_text(name, conf, expected):
    assert card.best_guess_text(name, conf) == expected
