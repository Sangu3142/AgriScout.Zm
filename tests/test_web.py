"""Phase 4b: the phone dashboard (robot/web) served by the Flask app."""

import json
import os
import re
import struct

import pytest

import shared
from fake_hardware import FakeRobot
from robot_server import WEB_DIR, RobotController, create_app

WEB_FILES = {
    "index.html": "text/html",
    "app.js": "javascript",
    "style.css": "text/css",
    "manifest.json": "json",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}


@pytest.fixture
def client():
    ctl = RobotController(FakeRobot())
    ctl.schedule = lambda delay, fn, *args: None
    yield create_app(ctl).test_client()
    ctl.shutdown()


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.mimetype == "text/html"
    html = r.get_data(as_text=True)
    assert "<title>AgriScout</title>" in html
    assert 'rel="manifest" href="/static/manifest.json"' in html
    assert 'src="/static/app.js"' in html


@pytest.mark.parametrize("name, mimetype", sorted(WEB_FILES.items()))
def test_static_files_load(client, name, mimetype):
    r = client.get("/static/" + name)
    assert r.status_code == 200
    assert mimetype in r.mimetype
    # Never cached for long: after a redeploy the phone must get the new app.js.
    cache = r.headers.get("Cache-Control", "")
    assert "max-age=43200" not in cache


def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])  # IHDR chunk
    return width, height


def test_manifest(client):
    manifest = json.loads(client.get("/static/manifest.json").get_data(as_text=True))
    assert manifest["name"] == "AgriScout"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest["theme_color"].startswith("#")
    sizes = set()
    for icon in manifest["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200
        w, h = png_size(r.get_data())
        assert "%dx%d" % (w, h) == icon["sizes"]
        sizes.add(w)
    assert {192, 512} <= sizes


def test_no_external_urls():
    """Tank Wi-Fi has no internet: every file must be local (no CDN, no web fonts)."""
    for name in os.listdir(WEB_DIR):
        if name.endswith((".html", ".js", ".css", ".json")):
            with open(os.path.join(WEB_DIR, name), encoding="utf-8") as f:
                text = f.read()
            assert not re.search(r"https?://|//cdn|@import|fonts\.googleapis", text), name


def test_pinned_controls_never_in_the_scroll_panel():
    """Status, banner, drive pad, STOP and modes sit in #pinned (never scrolls);
    the less important controls in #more-panel (the only part that scrolls)."""
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    start, split = html.index('id="pinned"'), html.index('id="more-panel"')
    pinned, more = html[start:split], html[split:]
    for needle in ('id="distance"', 'id="line"', 'id="banner"', 'id="pad"', 'id="stop"',
                   'data-mode="manual"', 'data-mode="line_follow"', 'data-mode="avoid"'):
        assert needle in pinned, needle
    for needle in ('id="speed"', 'id="pan"', 'id="tilt"', 'id="front"', 'id="scan"',
                   'class="led"', 'id="beep"'):
        assert needle in more, needle
    css = open(os.path.join(WEB_DIR, "style.css"), encoding="utf-8").read()
    assert "min-width: 1200px" in css      # wide screens: More in its own column


def test_video_detection_polls_natural_width():
    js = open(os.path.join(WEB_DIR, "app.js"), encoding="utf-8").read()
    assert "naturalWidth > 0" in js        # "load" is unreliable for MJPEG in Chrome
    css = open(os.path.join(WEB_DIR, "style.css"), encoding="utf-8").read()
    # Without this, "#no-video { display: flex }" beats the hidden attribute (the real bug).
    assert "[hidden] { display: none !important; }" in css


def test_api_404_still_json(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.get_json()["ok"] is False


def js_key_map():
    with open(os.path.join(WEB_DIR, "app.js"), encoding="utf-8") as f:
        text = f.read()
    block = re.search(r"const KEY_TO_CMD = \{(.*?)\};", text, re.S).group(1)
    return dict(re.findall(r"(\w+):\s*\"(\w+)\"", block))


def test_web_keys_match_desktop_gui():
    """Laptop browser and desktop GUI must use the same keys (W/A/S/D, Q/E)."""
    gui = pytest.importorskip("gui")  # desktop/gui.py (needs PySide6, on Windows only)
    desktop = {chr(int(key)).lower(): cmd for key, cmd in gui.KEY_TO_CMD.items()}
    assert js_key_map() == desktop
    assert set(desktop.values()) <= set(shared.DRIVE_TABLE)


def test_web_key_map_is_complete():
    keys = js_key_map()
    assert keys == {"w": "forward", "s": "back", "a": "left", "d": "right",
                    "q": "spin_left", "e": "spin_right"}
    js = open(os.path.join(WEB_DIR, "app.js"), encoding="utf-8").read()
    assert 'e.key === " "' in js          # Space = stop
    assert "e.repeat" in js               # key auto-repeat ignored


def test_climate_line_in_the_scroll_panel():
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert html.index('id="climate"') > html.index('id="more-panel"')  # not pinned
    js = open(os.path.join(WEB_DIR, "app.js"), encoding="utf-8").read()
    for needle in ("(sensor maximum)", "Sensor offline: ", "Waiting for the first reading...",
                   " (stale)", "const CLIMATE_FRESH_S = 30;", "climateText(s.climate)"):
        assert needle in js, needle


def test_soil_line_in_the_scroll_panel():
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert html.index('id="soil"') > html.index('id="climate"') > html.index('id="more-panel"')
    js = open(os.path.join(WEB_DIR, "app.js"), encoding="utf-8").read()
    for needle in ("soilText(s.soil)", " % moisture · ", " · raw ", "Soil: robot not reachable",
                   '"Soil: " + (s.reason || "unknown reason")'):
        assert needle in js, needle
    css = open(os.path.join(WEB_DIR, "style.css"), encoding="utf-8").read()
    assert "#soil.stale" in css


def test_web_climate_threshold_matches_desktop():
    from scout import config as scout_config
    js = open(os.path.join(WEB_DIR, "app.js"), encoding="utf-8").read()
    assert "const CLIMATE_FRESH_S = %d;" % scout_config.CLIMATE_FRESH_S in js
    assert "const CLIMATE_SENSOR_MAX_PCT = 90;" in js
