"""Paths and settings for scout/. Everything else imports its constants from here."""

import os

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "agriscout.db")
SCANS_DIR = os.path.join(DATA_DIR, "scans")      # scan photos, one JPEG per scan
STAKES_DIR = os.path.join(DATA_DIR, "stakes")    # printable QR stake sheets
REPORT_PATH = os.path.join(DATA_DIR, "report.html")
CONDITIONS_SEED = os.path.join(DATA_DIR, "conditions_seed.json")
PLANTS_FILE = os.path.join(DATA_DIR, "plants.json")
STUB_SCRIPT = os.path.join(DATA_DIR, "stub_script.json")
EXPORTS_DIR = os.path.join(DATA_DIR, "exports")  # result cards saved as PNG ("Save / Share")
CARDS_DIR = os.path.join(DATA_DIR, "cards")      # one HTML result card per scan, for report.html
DATASET_DIR = os.path.join(REPO_DIR, "dataset")  # training photos, dataset/<class>/<time>.jpg
MODELS_DIR = os.path.join(REPO_DIR, "models")
MODEL_PATH = os.path.join(MODELS_DIR, "plant_model.onnx")
LABELS_PATH = os.path.join(MODELS_DIR, "labels.txt")

# Below this confidence the scan says "uncertain" instead of trusting the diagnosis.
CONF_THRESHOLD = 0.6

DEFAULT_ROBOT_HOST = "192.168.50.1"
ROBOT_API_PORT = 5000        # robot_server.py (its /api/status has the DHT11 climate)
CLIMATE_TIMEOUT_S = 1.0      # asking the robot for its climate at scan time: never wait long
CAMERA_PORT = 8080           # mjpg-streamer on the Pi (started by its /etc/rc.local)
SNAPSHOT_TIMEOUT_S = 3.0     # one JPEG over the robot Wi-Fi; the stream can be slow to answer
SCOUT_PORT = 5050            # the laptop's scout server (robot uses 5000)
# DHT11 reading older than this: "stale" in the GUI / phone, and not stored with a scan.
CLIMATE_FRESH_S = 30


def snapshot_url_for(robot_address: str) -> str:
    """'192.168.50.1:5000' or 'http://192.168.50.1' -> 'http://192.168.50.1:8080/?action=snapshot'.

    Only the host is kept: the camera is always on port 8080, whatever port the robot API uses.
    """
    return "http://%s:%d/?action=snapshot" % (_host(robot_address), CAMERA_PORT)


def status_url_for(robot_address: str) -> str:
    """'192.168.50.1' -> 'http://192.168.50.1:5000/api/status' (the robot API, for its climate).
    Unlike the camera, the API port given is kept: '127.0.0.1:5097' (a --fake server) stays 5097."""
    address = robot_address.strip()
    if "://" in address:
        address = address.split("://", 1)[1]
    host_port = address.split("/", 1)[0]
    port = host_port.split(":", 1)[1] if ":" in host_port else ""
    return "http://%s:%s/api/status" % (_host(robot_address),
                                        port if port.isdigit() else ROBOT_API_PORT)


def _host(robot_address: str) -> str:
    """Only the host part: 'http://192.168.50.1:5000/x' -> '192.168.50.1'."""
    host = robot_address.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.split("/", 1)[0].split(":", 1)[0] or DEFAULT_ROBOT_HOST


def stream_url_for(robot_address: str) -> str:
    """Same host, but mjpg-streamer's live MJPEG stream (for a preview window)."""
    return snapshot_url_for(robot_address).replace("action=snapshot", "action=stream")
