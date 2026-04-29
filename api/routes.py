"""
server/api/routes.py — Flask API Endpoints

CHANGES IN THIS VERSION:
  - /api/pull/visits (GET): NEW endpoint — the desktop SyncEngine calls this
    to download visits that were created on the web dashboard so they appear
    in the local SQLite database too (two-way sync).
"""

import os
from flask import Blueprint, request, jsonify
from data.server_db import (
    upsert_visit, upsert_checkout, upsert_passenger,
    get_active_visits_server, get_visit_history_server,
    get_stats_server, init_server_db, get_visits_for_pull
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
    return jsonify({"status": "ok", "message": "Server is running"}), 200


# ── DEBUG ENDPOINT ─────────────────────────────────────────────────────────
@api_bp.route("/api/debug", methods=["GET"])
def debug():
    try:
        init_server_db()
        db_status = "tables ready"
    except Exception as e:
        db_status = f"error: {str(e)}"

    key_set    = API_SECRET_KEY != "change-this-to-a-long-random-string-before-deploying"
    key_prefix = API_SECRET_KEY[:6] + "..." if len(API_SECRET_KEY) > 6 else "NOT SET"

    return jsonify({
        "api_key_configured": key_set,
        "api_key_prefix":     key_prefix,
        "database_status":    db_status,
        "hint": "Make sure your desktop config.py API_SECRET_KEY matches api_key_prefix"
    }), 200


# ── SYNC PUSH ENDPOINTS (desktop → server) ────────────────────────────────

@api_bp.route("/api/sync/visit", methods=["POST"])
def sync_visit():
    if not _check_api_key():
        received = request.headers.get("X-API-Key", "MISSING")
        print(f"[Auth] REJECTED. Received key prefix: {received[:6]}... "
              f"Expected prefix: {API_SECRET_KEY[:6]}...")
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
        print(f"[Sync] Visit saved: {data['full_name']} | {data['log_uuid'][:8]}")
        return jsonify({"status": "saved", "log_uuid": data["log_uuid"]}), 201
    else:
        return jsonify({"error": "Database error — check Railway logs"}), 500


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


# ── PULL ENDPOINT (server → desktop, two-way sync) ─────────────────────────

@api_bp.route("/api/pull/visits", methods=["GET"])
def pull_visits():
    """
    Desktop SyncEngine calls this GET endpoint to download visits that were
    created on the web dashboard so they appear in the local SQLite DB too.

    Query param: ?since=2026-04-15+12:00:00  (optional)
      If provided, only visits newer than this timestamp are returned.
      The desktop passes its most recent known check_in_time so it only
      downloads new records, not the entire history every cycle.

    Returns JSON:
      { "visits": [ { log_uuid, visitor_uuid, full_name, ... }, ... ] }
    """
    if not _check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    since = request.args.get("since", None)
    visits = get_visits_for_pull(since=since)
    return jsonify({"visits": visits, "count": len(visits)}), 200


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