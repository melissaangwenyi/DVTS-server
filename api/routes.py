"""
server/api/routes.py — Flask API Endpoints
"""

import os
from flask import Blueprint, request, jsonify
from data.server_db import (
    upsert_visit, upsert_checkout, upsert_passenger,
    get_active_visits_server, get_visit_history_server, get_stats_server
)

api_bp = Blueprint("api", __name__)

API_SECRET_KEY = os.environ.get(
    "API_SECRET_KEY",
    "change-this-to-a-long-random-string-before-deploying"
)


def _check_api_key() -> bool:
    provided_key = request.headers.get("X-API-Key", "")
    return provided_key == API_SECRET_KEY


# ── HEALTH CHECK — no API key required ────────────────────────────────────
@api_bp.route("/api/health", methods=["GET"])
def health():
    """
    Simple ping — no API key needed.
    The desktop SyncEngine calls this to check if server is reachable.
    """
    return jsonify({"status": "ok", "message": "Server is running"}), 200


# ── SYNC ENDPOINTS ─────────────────────────────────────────────────────────

@api_bp.route("/api/sync/visit", methods=["POST"])
def sync_visit():
    if not _check_api_key():
        return jsonify({"error": "Unauthorized — wrong API key"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON data received"}), 400

    required = ["log_uuid", "visitor_uuid", "full_name", "category", "check_in_time"]
    missing  = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    success = upsert_visit(data)
    if success:
        return jsonify({"status": "saved", "log_uuid": data["log_uuid"]}), 201
    else:
        return jsonify({"error": "Database error"}), 500


@api_bp.route("/api/sync/checkout", methods=["POST"])
def sync_checkout():
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    data           = request.get_json()
    log_uuid       = data.get("log_uuid")
    check_out_time = data.get("check_out_time")

    if not log_uuid or not check_out_time:
        return jsonify({"error": "Missing log_uuid or check_out_time"}), 400

    success = upsert_checkout(log_uuid, check_out_time)
    if success:
        return jsonify({"status": "updated"}), 200
    else:
        return jsonify({"error": "Database error"}), 500


@api_bp.route("/api/sync/passenger", methods=["POST"])
def sync_passenger():
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    data        = request.get_json()
    log_uuid    = data.get("log_uuid")
    national_id = data.get("national_id")

    if not log_uuid or not national_id:
        return jsonify({"error": "Missing fields"}), 400

    success = upsert_passenger(log_uuid, national_id)
    if success:
        return jsonify({"status": "saved"}), 201
    else:
        return jsonify({"error": "Database error"}), 500


# ── DASHBOARD READ ENDPOINTS ───────────────────────────────────────────────

@api_bp.route("/api/dashboard/active", methods=["GET"])
def dashboard_active():
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    visits = get_active_visits_server()
    return jsonify({"active_visits": visits, "count": len(visits)}), 200


@api_bp.route("/api/dashboard/history", methods=["GET"])
def dashboard_history():
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    history = get_visit_history_server()
    return jsonify({"history": history, "count": len(history)}), 200


@api_bp.route("/api/dashboard/stats", methods=["GET"])
def dashboard_stats():
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    stats = get_stats_server()
    return jsonify(stats), 200