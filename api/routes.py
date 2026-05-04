"""server/api/routes.py — Public health check only."""

from flask import Blueprint, jsonify

api_bp = Blueprint("api", __name__)


@api_bp.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Visitor Tracking System is running"}), 200