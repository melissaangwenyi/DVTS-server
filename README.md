# Digital Visitor Tracking System — Server

The Flask-based web backend for the Digital Visitor Tracking System. Provides a REST API that receives synced records from the desktop client, and a full web dashboard for administrators to monitor visits, manage guards and residents, view audit logs, and export reports.

## Overview

The server runs on Railway and acts as the central hub for all DVTS desktop installations. Each desktop client syncs its local records to this server over HTTPS using a shared API key. Administrators access the web dashboard from any browser to get a real-time view across all locations.

## Features

**Web Dashboard**
- Live overview of all active visitors across registered premises
- Historical visit log with filtering by date, guard, and visitor category
- CSV export for any filtered date range
- Guard management: add, activate, deactivate, and reset passwords
- Resident/host management with support for office and residential property types
- Optional email notification to host on visitor check-in (Gmail SMTP)

**Security**
- Session-based authentication with 30-minute idle timeout
- Role-based access: guard role vs admin role
- API key validation on all desktop sync endpoints
- Blacklist management: flag visitors by National ID; admin override available at check-in
- Full audit log recording every significant action with guard identity and timestamp

**API Endpoints (for desktop sync)**
- `POST /api/sync/visit` — receive a new visit record from the desktop
- `POST /api/sync/passenger` — receive a passenger record
- `GET /api/autofill/<national_id>` — return autofill data for repeat visitors

**Additional Features**
- Pre-registration: hosts can register expected visitors in advance
- Visitor autofill: repeat visitors are looked up by National ID to speed up check-in
- Dark/light theme toggle

## Tech Stack

- **Language:** Python
- **Framework:** Flask 3.0
- **Database:** SQLite (local) / PostgreSQL-compatible via psycopg2
- **Deployment:** Railway (Gunicorn)
- **Email:** Gmail SMTP (optional)

## Project Structure

```
DVTS-server/
├── app.py                  # Main Flask application and all web routes
├── Procfile                # Railway deployment configuration
├── requirements.txt        # Dependencies
├── api/
│   └── routes.py           # REST API blueprint (desktop sync endpoints)
├── data/
│   ├── server_db.py        # All database operations
│   └── email_service.py    # Gmail SMTP notification service
├── templates/              # Jinja2 HTML templates
│   ├── dashboard.html
│   ├── reports.html
│   ├── manage_guards.html
│   ├── manage_hosts.html
│   ├── blacklist.html
│   └── audit_log.html
└── static/
    ├── styles.css
    └── theme.js
```

## Deployment (Railway)

```bash
# Install Railway CLI, then:
railway login
railway init
railway up
```

Set the following environment variables in Railway:
```
SECRET_KEY=your-secret-key
API_SECRET_KEY=your-shared-key-matching-desktop-config
GMAIL_USER=your-email@gmail.com        # optional
GMAIL_APP_PASSWORD=your-app-password   # optional
```

## Local Development

```bash
git clone https://github.com/melissaangwenyi/DVTS-server.git
cd DVTS-server
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:5000`

## Related Repository

The desktop client lives at [DVTS](https://github.com/melissaangwenyi/DVTS).

## Author

Melissa Angwenyi — melissaangwenyi276@gmail.com



