"""
=============================================================================
server/api/routes.py  —  Flask API Endpoints (Routes)
Location: server/api/routes.py

PURPOSE:
    Defines all the web addresses (URLs) the desktop app can call.
    Each function here is triggered when a specific URL is visited.

WHAT IS A ROUTE:
    A route is a URL + an action.
    Example:
      URL:    POST https://your-server.railway.app/api/sync/visit
      Action: receive visit data → save to PostgreSQL → reply "OK"

    The desktop app's ApiClient sends to these URLs.
    This file receives them and saves to PostgreSQL via server_db.py.

SECURITY:
    Every route checks the X-API-Key header.
    If it doesn't match the API_SECRET_KEY environment variable,
    the server replies with HTTP 401 (Unauthorized) and ignores the request.
    This prevents random people from posting fake data to your server.

HTTP STATUS CODES explained:
    200 = OK (success, no new content created)
    201 = Created (success, new record saved)
    400 = Bad Request (data was missing or wrong format)
    401 = Unauthorized (wrong or missing API key)
    500 = Internal Server Error (something crashed on the server)
=============================================================================
"""

import os
from flask import Blueprint, request, jsonify
from data.server_db import (
    upsert_visit, upsert_checkout, upsert_passenger,
    get_active_visits_server, get_visit_history_server, get_stats_server
)

# A Blueprint is Flask's way of grouping related routes together.
# We register this blueprint in app.py.
api_bp = Blueprint("api", __name__)

# Read the secret key from environment variables (set on Railway).
# Never hardcode secrets — keep them in environment variables.
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "change-this-to-a-long-random-string-before-deploying")


def _check_api_key() -> bool:
    """
    Checks that the request includes the correct API key in its headers.
    Called at the top of every protected route.
    Returns True if key is valid, False if missing or wrong.
    """
    provided_key = request.headers.get("X-API-Key", "")
    return provided_key == API_SECRET_KEY


# ── HEALTH CHECK ───────────────────────────────────────────────────────────

@api_bp.route("/api/health", methods=["GET"])
def health():
    """
    Simple ping endpoint. The desktop SyncEngine calls this first
    to check if the server is reachable before attempting a full sync.
    Returns HTTP 200 with a JSON message.
    No API key required — we just want to know if the server is alive.
    """
    return jsonify({"status": "ok", "message": "Server is running"}), 200


# ── SYNC ENDPOINTS (called by the desktop SyncEngine) ─────────────────────

@api_bp.route("/api/sync/visit", methods=["POST"])
def sync_visit():
    """
    Receives a complete visit record from the desktop app.
    The desktop sends JSON in the request body. Flask reads it with
    request.get_json() which converts the JSON string back to a Python dict.

    Expected JSON fields:
        log_uuid, visitor_uuid, full_name, national_id, phone_number,
        vehicle_plate, category, exception_flag, check_in_time,
        check_out_time, pax_count, guard_id, resident_id
    """
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON data received"}), 400

    # Validate the minimum required fields
    required = ["log_uuid", "visitor_uuid", "full_name", "category", "check_in_time"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    success = upsert_visit(data)
    if success:
        return jsonify({"status": "saved", "log_uuid": data["log_uuid"]}), 201
    else:
        return jsonify({"error": "Database error"}), 500


@api_bp.route("/api/sync/checkout", methods=["POST"])
def sync_checkout():
    """
    Receives a checkout update: just the UUID and the exit time.
    Called when a guard checked someone out while offline and now syncing.

    Expected JSON fields:
        log_uuid, check_out_time
    """
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON data received"}), 400

    log_uuid       = data.get("log_uuid")
    check_out_time = data.get("check_out_time")

    if not log_uuid or not check_out_time:
        return jsonify({"error": "Missing log_uuid or check_out_time"}), 400

    success = upsert_checkout(log_uuid, check_out_time)
    if success:
        return jsonify({"status": "updated", "log_uuid": log_uuid}), 200
    else:
        return jsonify({"error": "Database error"}), 500


@api_bp.route("/api/sync/passenger", methods=["POST"])
def sync_passenger():
    """
    Receives one passenger record (group check-in).

    Expected JSON fields:
        log_uuid, national_id
    """
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON data received"}), 400

    log_uuid    = data.get("log_uuid")
    national_id = data.get("national_id")

    if not log_uuid or not national_id:
        return jsonify({"error": "Missing log_uuid or national_id"}), 400

    success = upsert_passenger(log_uuid, national_id)
    if success:
        return jsonify({"status": "saved"}), 201
    else:
        return jsonify({"error": "Database error"}), 500


# ── WEB DASHBOARD ENDPOINTS (read-only, for viewing data in a browser) ─────

@api_bp.route("/api/dashboard/active", methods=["GET"])
def dashboard_active():
    """
    Returns all currently active visits as JSON.
    A supervisor can call this URL in a browser or build a web dashboard
    that reads from it.
    """
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    visits = get_active_visits_server()
    return jsonify({"active_visits": visits, "count": len(visits)}), 200


@api_bp.route("/api/dashboard/history", methods=["GET"])
def dashboard_history():
    """Returns completed visit history."""
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    history = get_visit_history_server()
    return jsonify({"history": history, "count": len(history)}), 200


@api_bp.route("/api/dashboard/stats", methods=["GET"])
def dashboard_stats():
    """Returns summary statistics."""
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    stats = get_stats_server()
    return jsonify(stats), 200