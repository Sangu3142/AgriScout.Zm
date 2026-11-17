"""Small local web server for scouting (laptop, port 5050). Separate from the robot's server.

  POST /scan            scan the batch in front of the camera  (JSON body optional: {"plant_id": "T-01"})
  GET  /scans           newest scans                           (?plant_id=T-01&limit=20)
  GET  /plants/<id>     one batch + its scans
  GET  /report          field report (HTML)
  GET  /cards/<id>.html full result card of one scan (the report's "View" links)
  GET  /scans/<file>    a scan photo

Usage (repo root):
  python -m scout.server                        robot camera at 192.168.50.1, report on this laptop
  python -m scout.server --host 0.0.0.0         also reachable from a phone on the robot hotspot
Offline: POST /scan answers 502 "no camera snapshot" (nothing is saved).
"""

import argparse
from typing import Callable, Optional

from flask import Flask, abort, jsonify, request, send_from_directory

from scout import config, db
from scout.card import card_data, render_card
from scout.classifier import Classifier
from scout.pipeline import ScanError, fetch_climate, make_classifier, scan_plant
from scout.qr import PLANT_ID_RE
from scout.report import render_report

MAX_SCANS_LIMIT = 500


def create_app(classifier: Classifier, db_path: str = config.DB_PATH,
               snapshot_url: Optional[str] = None, scans_dir: str = config.SCANS_DIR,
               get_image: Optional[Callable[[], bytes]] = None,
               get_climate: Optional[Callable[[], Optional[dict]]] = None) -> Flask:
    """get_image: returns photo bytes instead of the snapshot URL (tests).
    get_climate: returns the robot's /api/status "climate" (or None) at scan time."""
    app = Flask(__name__)

    def error(status: int, message: str):
        return jsonify(ok=False, error=message), status

    @app.route("/scan", methods=["POST"])
    def scan():
        body = request.get_json(silent=True) or {}
        plant_id = body.get("plant_id")
        if plant_id is not None and (not isinstance(plant_id, str)
                                     or not PLANT_ID_RE.match(plant_id)):
            return error(400, "plant_id must look like T-01")
        try:
            image = get_image() if get_image else None
            climate = get_climate() if get_climate else None
            result = scan_plant(classifier, db_path, snapshot_url=snapshot_url,
                                image_bytes=image, plant_id=plant_id, scans_dir=scans_dir,
                                climate=climate)
        except ScanError as err:
            return error(502, str(err))
        result.pop("image_file")  # a laptop file path: no use to a browser
        return jsonify(result)

    @app.route("/scans", methods=["GET"])
    def scans():
        try:
            limit = min(max(int(request.args.get("limit", 50)), 1), MAX_SCANS_LIMIT)
        except ValueError:
            return error(400, "limit must be a number")
        conn = db.open_db(db_path)
        try:
            rows = db.list_scans(conn, request.args.get("plant_id"), limit)
        finally:
            conn.close()
        return jsonify(ok=True, scans=rows)

    @app.route("/plants/<plant_id>", methods=["GET"])
    def plant(plant_id):
        conn = db.open_db(db_path)
        try:
            info = db.get_plant(conn, plant_id)
            rows = db.list_scans(conn, plant_id)
        finally:
            conn.close()
        if info is None and not rows:
            return error(404, "no plant %s" % plant_id)
        return jsonify(ok=True, plant=info, scans=rows)

    @app.route("/report", methods=["GET"])
    def report():
        return render_report(db_path, image_base="/")

    @app.route("/cards/<int:scan_id>.html", methods=["GET"])
    def card(scan_id):
        conn = db.open_db(db_path)
        try:
            data = card_data(conn, scan_id)
        finally:
            conn.close()
        if data is None:
            abort(404)
        return render_card(data, "report", image_base="/", back_href="/report")

    @app.route("/scans/<path:name>", methods=["GET"])
    def photo(name):
        # send_from_directory refuses names that leave scans_dir (../ etc.) with a 404.
        if not name.lower().endswith(".jpg"):
            abort(404)
        return send_from_directory(scans_dir, name)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="AgriScout scouting server (laptop)")
    parser.add_argument("--robot", default=config.DEFAULT_ROBOT_HOST, help="robot host")
    parser.add_argument("--host", default="127.0.0.1",
                        help="listen address (0.0.0.0 = reachable from other devices)")
    parser.add_argument("--port", type=int, default=config.SCOUT_PORT)
    args = parser.parse_args()

    db.setup()
    classifier = make_classifier()
    snapshot_url = config.snapshot_url_for(args.robot)
    print("classifier: %s, camera: %s" % (classifier.name, snapshot_url))
    print("report: http://%s:%d/report" % (args.host, args.port))
    create_app(classifier, snapshot_url=snapshot_url,
               get_climate=lambda: fetch_climate(args.robot)).run(host=args.host, port=args.port,
                                                                  threaded=True)


if __name__ == "__main__":
    main()
