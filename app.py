"""
=============================================================================
server/app.py  —  Flask Server Entry Point
Location: server/app.py

PURPOSE:
    Starts the Flask web server. This is the file Railway runs to
    start your server in the cloud.

WHAT FLASK IS:
    Flask is a Python library that turns your Python functions into
    a web server. When the server starts, Flask listens for incoming
    HTTP requests and routes them to the right function in routes.py.

HOW RAILWAY RUNS THIS:
    Railway reads the Procfile, which says:
        web: gunicorn app:app
    This means: run the variable named 'app' inside the file 'app.py'
    using gunicorn (a production-grade web server).

    'app' is the Flask() instance created at the bottom of this file.
=============================================================================
"""

import os
import sys

# Make sure Python can find our server subfolders
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask
from api.routes import api_bp
from data.server_db import init_server_db

# Create the Flask application instance.
# __name__ tells Flask where to look for templates and static files.
app = Flask(__name__)
app.register_blueprint(api_bp)

# Initialise database tables on startup
with app.app_context():
    try:
        init_server_db()
    except Exception as e:
        print(f"[Startup] DB init warning: {e}")


@app.route("/")
def index():
    """
    Root URL — just confirms the server is running.
    Visit https://your-server.railway.app/ in a browser to see this.
    """
    return {
        "project":   "Digital Visitor Tracking System",
        "developer": "Angwenyi Melissa Moraa — SCS3/149260/2024",
        "university":"University of Nairobi",
        "status":    "Server is running",
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
    # This block only runs when you do: python app.py (local testing)
    # Railway uses gunicorn instead (defined in Procfile).

    # Create all PostgreSQL tables if they don't exist yet
    try:
        init_server_db()
    except Exception as e:
        print(f"[app.py] Warning: Could not initialise database: {e}")
        print("[app.py] Make sure DATABASE_URL is set correctly.")

    # Start Flask development server on port 5000
    # debug=True means the server restarts automatically when you edit code
    # host="0.0.0.0" means it listens on all network interfaces
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)