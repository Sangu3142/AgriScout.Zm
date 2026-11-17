"""Small wrapper around the AgriScout robot API (robot/robot_server.py).

Every method returns a Result and never raises, so a network problem can't crash the GUI.
One ApiClient per thread: requests.Session is not guaranteed to be thread-safe.
"""

from dataclasses import dataclass, field

import requests

DEFAULT_ADDRESS = "192.168.50.1:5000"
DEFAULT_TIMEOUT_S = 0.5
SCAN_TIMEOUT_S = 6.0  # a scan moves the servo 6 times (0.3 s each) + readings: ~2.5 s on the Pi


@dataclass
class Result:
    ok: bool
    data: dict = field(default_factory=dict)
    error: str | None = None
    status: int | None = None  # HTTP status, None if we never got a reply


NO_CLIMATE_REASON = "robot server has no climate data (deploy the new version)"


@dataclass
class Climate:
    """The "climate" part of /api/status (robot/climate.py). Numbers are None when missing."""
    available: bool
    temp_c: float | None = None
    humidity_pct: float | None = None
    age_s: float | None = None
    ok: int = 0
    failed: int = 0
    last_error: str | None = None
    reason: str | None = None


def _number(value) -> float | None:
    # bool is a subclass of int: True must not count as 1 degree.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


def parse_climate(status: dict) -> Climate:
    """Climate from a /api/status reply. Never raises: an older robot server (no "climate" key)
    or a bad value gives an offline / partly empty Climate instead."""
    c = status.get("climate") if isinstance(status, dict) else None
    if not isinstance(c, dict):
        return Climate(False, reason=NO_CLIMATE_REASON)
    return Climate(available=c.get("available") is True,
                   temp_c=_number(c.get("temp_c")),
                   humidity_pct=_number(c.get("humidity_pct")),
                   age_s=_number(c.get("age_s")),
                   ok=_count(c.get("ok")),
                   failed=_count(c.get("failed")),
                   last_error=_text(c.get("last_error")),
                   reason=_text(c.get("reason")))


NO_SOIL_REASON = "robot server has no soil data (deploy the new version)"


@dataclass
class Soil:
    """The "soil" part of /api/status (robot/soil.py). Values are None when missing."""
    available: bool
    raw: int | None = None
    moisture_pct: int | None = None
    state: str | None = None  # "dry", "ok" or "wet"
    age_s: float | None = None
    reason: str | None = None


def parse_soil(status: dict) -> Soil:
    """Soil from a /api/status reply. Never raises: an older robot server (no "soil" key) or a
    bad value gives an offline / partly empty Soil instead."""
    s = status.get("soil") if isinstance(status, dict) else None
    if not isinstance(s, dict):
        return Soil(False, reason=NO_SOIL_REASON)
    raw, pct = _number(s.get("raw")), _number(s.get("moisture_pct"))
    state = s.get("state")
    return Soil(available=s.get("available") is True,
                raw=None if raw is None else int(raw),
                moisture_pct=None if pct is None else int(pct),
                state=state if state in ("dry", "ok", "wet") else None,
                age_s=_number(s.get("age_s")),
                reason=_text(s.get("reason")))


def normalize_address(address: str) -> str:
    """'192.168.50.1:5000' -> 'http://192.168.50.1:5000' (keeps an existing http://, drops a trailing /)."""
    address = address.strip().rstrip("/")
    if not address.startswith(("http://", "https://")):
        address = "http://" + address
    return address


class ApiClient:
    def __init__(self, address: str = DEFAULT_ADDRESS, timeout: float = DEFAULT_TIMEOUT_S):
        self.base_url = normalize_address(address)
        self.timeout = timeout
        # One Session = one kept-alive TCP connection, so each request skips the TCP handshake
        # (noticeably faster over the robot Wi-Fi).
        self.session = requests.Session()

    def close(self) -> None:
        self.session.close()

    # ---------- one method per route ----------

    def drive(self, cmd: str, speed: float | None = None) -> Result:
        body = {"cmd": cmd}
        if speed is not None:
            body["speed"] = speed
        return self._request("POST", "/api/drive", body)

    def camera(self, pan: float | None = None, tilt: float | None = None) -> Result:
        body = {}
        if pan is not None:
            body["pan"] = pan
        if tilt is not None:
            body["tilt"] = tilt
        return self._request("POST", "/api/camera", body)

    def front_servo(self, angle: float) -> Result:
        return self._request("POST", "/api/front_servo", {"angle": angle})

    def beep(self, seconds: float = 0.2) -> Result:
        return self._request("POST", "/api/beep", {"seconds": seconds})

    def led(self, r: int, g: int, b: int) -> Result:
        return self._request("POST", "/api/led", {"r": r, "g": g, "b": b})

    def mode(self, name: str) -> Result:
        return self._request("POST", "/api/mode", {"mode": name})

    def scan(self) -> Result:
        return self._request("POST", "/api/scan", {}, timeout=SCAN_TIMEOUT_S)

    def status(self) -> Result:
        return self._request("GET", "/api/status")

    def config(self) -> Result:
        return self._request("GET", "/api/config")

    # ---------- the only place that talks HTTP ----------

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float | None = None) -> Result:
        url = self.base_url + path
        timeout = timeout or self.timeout
        try:
            reply = self.session.request(method, url, json=body, timeout=timeout)
        except requests.ConnectTimeout:  # no connection at all (robot off, wrong address)
            return Result(False, error=f"cannot reach robot at {self.base_url} (timed out)")
        except requests.Timeout:         # connected, but the reply was too slow
            return Result(False, error=f"robot did not answer within {timeout} s")
        except requests.ConnectionError:  # e.g. connection refused: nothing on that port
            return Result(False, error=f"cannot reach robot at {self.base_url}")
        except requests.RequestException as err:  # bad URL, etc.
            return Result(False, error=f"request failed: {err}")

        try:
            data = reply.json()
        except ValueError:
            return Result(False, error=f"bad reply (not JSON) from {url}", status=reply.status_code)
        if not isinstance(data, dict):
            return Result(False, error=f"bad reply (not a JSON object) from {url}",
                          status=reply.status_code)

        if reply.ok:
            return Result(True, data=data, status=reply.status_code)
        # The server always answers errors as {"ok": false, "error": "..."}.
        message = data.get("error") or f"HTTP {reply.status_code}"
        return Result(False, data=data, error=message, status=reply.status_code)
