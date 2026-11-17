"""Soil panel (desktop/gui.py soil_text) and api_client.parse_soil."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gui  # noqa: E402
from api_client import NO_SOIL_REASON, Soil, parse_soil  # noqa: E402

WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "robot", "web")


def reading(raw=600, pct=57, state="ok", age=1.3, available=True, reason=None):
    return Soil(available, raw=raw, moisture_pct=pct, state=state, age_s=age, reason=reason)


# ---------------------------------------------------------------- soil_text

def test_fresh_reading():
    assert gui.soil_text(reading()) == ("57 % moisture · OK · raw 600 · updated 1 s ago", "fresh")


@pytest.mark.parametrize("state, word", [("dry", "Dry"), ("ok", "OK"), ("wet", "Wet")])
def test_state_words(state, word):
    assert f"· {word} ·" in gui.soil_text(reading(state=state))[0]


def test_stale_reading_is_grey_with_the_reason():
    s = reading(age=12.4, available=False, reason="no reading for 12 s")
    assert gui.soil_text(s) == ("57 % moisture · OK · raw 600 (no reading for 12 s)", "stale")


def test_uno_not_connected():
    s = Soil(False, reason="Uno not connected")
    assert gui.soil_text(s) == ("Soil: Uno not connected", "offline")


def test_waiting_and_unknown_reason():
    assert gui.soil_text(Soil(False, reason="waiting for the Uno"))[1] == "offline"
    assert gui.soil_text(Soil(False)) == ("Soil: unknown reason", "offline")


# ---------------------------------------------------------------- parse_soil

def test_parse_soil():
    status = {"soil": {"available": True, "raw": 734, "moisture_pct": 38, "state": "ok",
                       "age_s": 0.5, "reason": None}}
    assert parse_soil(status) == Soil(True, 734, 38, "ok", 0.5, None)


def test_parse_old_robot_server_without_soil():
    s = parse_soil({"mode": "manual"})
    assert s == Soil(False, reason=NO_SOIL_REASON)
    assert gui.soil_text(s)[1] == "offline"


@pytest.mark.parametrize("bad", [None, "x", [], {"soil": "wet"}, {"soil": None}])
def test_parse_soil_never_raises(bad):
    assert parse_soil(bad).available is False


def test_parse_soil_bad_values():
    s = parse_soil({"soil": {"available": "yes", "raw": "600", "moisture_pct": True,
                             "state": "soggy", "age_s": "old", "reason": 5}})
    assert s == Soil(False)


# ---------------------------------------------------------------- phone page uses the same words

def test_web_soil_words_match_desktop():
    with open(os.path.join(WEB_DIR, "app.js"), encoding="utf-8") as f:
        js = f.read()
    words = ", ".join('%s: "%s"' % kv for kv in gui.SOIL_STATE_TEXT.items())
    assert "const SOIL_STATE_TEXT = { %s };" % words in js
