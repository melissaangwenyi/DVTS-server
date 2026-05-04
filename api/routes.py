"""
server/api/routes.py — Minimal API blueprint

All desktop-sync endpoints have been removed. The only thing that remains is a
public health-check endpoint, which Railway uses to confirm the app is alive.

If you ever want to expose JSON endpoints again (mobile app, integrations,
etc.) add them here behind whatever auth scheme you prefer.
"""

from flask import Blueprint, jsonify

api_bp = Blueprint("api", __name__)


@api_bp.route("/api/health", methods=["GET"])
def health():
    """Health check — used by Railway and uptime monitors."""
    return jsonify({"status": "ok", "message": "Visitor Tracking System is running"}), 200