"""Climate panel (desktop/gui.py climate_text) and api_client.parse_climate."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gui  # noqa: E402
from api_client import NO_CLIMATE_REASON, Climate, parse_climate  # noqa: E402


def reading(temp=28.6, hum=57.0, age=4.2):
    return Climate(True, temp_c=temp, humidity_pct=hum, age_s=age, ok=10)


# ---------------------------------------------------------------- climate_text

def test_fresh():
    assert gui.climate_text(reading()) == ("28.6 °C · 57 % RH · updated 4 s ago", "fresh")


def test_exactly_30_s_is_still_fresh():
    assert gui.climate_text(reading(age=30.0))[1] == "fresh"


def test_stale_after_30_s():
    assert gui.climate_text(reading(age=45.9)) == (
        "28.6 °C · 57 % RH · updated 45 s ago (stale)", "stale")


def test_very_old_reading_in_minutes():
    text, state = gui.climate_text(reading(age=150))
    assert text.endswith("updated 2 min ago (stale)") and state == "stale"


@pytest.mark.parametrize("hum", [90.0, 91.0, 95.0])
def test_humidity_at_sensor_maximum(hum):
    text, state = gui.climate_text(reading(hum=hum))
    assert text == "28.6 °C · ≥ 90 % RH (sensor maximum) · updated 4 s ago"
    assert state == "fresh"


def test_89_is_a_normal_reading():
    assert "89 % RH ·" in gui.climate_text(reading(hum=89.0))[0]


def test_offline_shows_the_reason():
    c = Climate(False, reason="pigpiod not running - start it with: sudo pigpiod -l -p 8889")
    assert gui.climate_text(c) == (
        "Sensor offline: pigpiod not running - start it with: sudo pigpiod -l -p 8889", "offline")


def test_offline_with_an_old_reading_is_still_offline():
    c = Climate(False, temp_c=24.5, humidity_pct=61.0, age_s=6.0, reason="lost pigpiod: reset")
    assert gui.climate_text(c) == ("Sensor offline: lost pigpiod: reset", "offline")


def test_offline_without_reason():
    assert gui.climate_text(Climate(False)) == ("Sensor offline: unknown reason", "offline")


def test_waiting_for_first_reading():
    assert gui.climate_text(Climate(True)) == ("Waiting for the first reading...", "waiting")


# ---------------------------------------------------------------- parse_climate

def test_parse_full():
    status = {"climate": {"available": True, "temp_c": 24.0, "humidity_pct": 60, "age_s": 1.2,
                          "ok": 3, "failed": 1, "last_error": "checksum error", "reason": None}}
    assert parse_climate(status) == Climate(True, 24.0, 60.0, 1.2, 3, 1, "checksum error", None)


def test_parse_old_robot_server_without_climate():
    c = parse_climate({"mode": "manual"})
    assert c == Climate(False, reason=NO_CLIMATE_REASON)
    assert gui.climate_text(c)[1] == "offline"


@pytest.mark.parametrize("bad", [None, "x", [], {"climate": "hot"}, {"climate": None}])
def test_parse_garbage_never_raises(bad):
    assert parse_climate(bad).available is False


def test_parse_bad_values():
    c = parse_climate({"climate": {"available": "yes", "temp_c": "hot", "humidity_pct": True,
                                   "age_s": None, "ok": 2.5, "failed": False, "reason": 7}})
    assert c == Climate(False)  # "yes" is not True, wrong types become None / 0


# ---------------------------------------------------------------- climate at scan time

def test_climate_for_scan_moves_the_age_on():
    c = reading(age=4.0)
    data = gui.climate_for_scan((c, 100.0), now=100.3)
    assert data["available"] is True and data["temp_c"] == 28.6
    assert data["age_s"] == pytest.approx(4.3)


def test_climate_for_scan_goes_stale_when_the_robot_stops_answering():
    from scout import storage
    data = gui.climate_for_scan((reading(age=4.0), 100.0), now=130.0)   # no new status for 30 s
    assert data["age_s"] == pytest.approx(34.0)
    assert storage.climate_columns(data) == (None, None, None)          # not stored with the scan


def test_climate_for_scan_without_status_yet():
    assert gui.climate_for_scan(None, now=5.0) is None
    data = gui.climate_for_scan((Climate(False, reason="x"), 1.0), now=2.0)
    assert data["available"] is False and data["age_s"] is None
