import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask
from api.routes import api_bp
from data.server_db import init_server_db

app = Flask(__name__)
app.register_blueprint(api_bp)

# This runs every time the server starts — creates tables if they don't exist
try:
    init_server_db()
    print("[Startup] Database tables ready.")
except Exception as e:
    print(f"[Startup] DB init warning: {e}")


@app.route("/")
def index():
    return {
        "project":    "Digital Visitor Tracking System",
        "developer":  "Angwenyi Melissa Moraa — SCS3/149260/2024",
        "university": "University of Nairobi",
        "status":     "Server is running",
        "endpoints": [
            "GET  /api/health",
            "POST /api/sync/visit",
            "POST /api/sync/checkout",
            "POST /api/sync/passenger",
            "GET  /api/dashboard/active",
            "GET  /api/dashboard/history",
            "GET  /api/dashboard/stats",
        ]
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)