"""
server/app.py — Flask Server with full web interface
"""
import os, sys, uuid, hashlib
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, jsonify)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.routes import api_bp
from data.server_db import (
    init_server_db, get_active_visits_server,
    get_filtered_history, get_stats_server,
    web_checkout, upsert_visit
)

app = Flask(__name__)

# Secret key for sessions — Flask uses this to encrypt the session cookie
app.secret_key = os.environ.get("SECRET_KEY", "vts-secret-key-change-in-production")

# Register API blueprint (for desktop sync)
app.register_blueprint(api_bp)

# Web login password — set this as an environment variable on Railway
# This is separate from guard passwords on the desktop app
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "admin123")

# Initialise database tables on startup
try:
    init_server_db()
    print("[Startup] Database tables ready.")
except Exception as e:
    print(f"[Startup] DB init warning: {e}")


# ── LOGIN REQUIRED DECORATOR ───────────────────────────────────────────────
def login_required(f):
    """
    Wraps any route that needs the guard to be logged in.
    If not logged in, redirects to the login page.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if "guard_name" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── HELPER: calculate duration string ─────────────────────────────────────
def duration_str(check_in_str, check_out_str=None):
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        ci  = datetime.strptime(str(check_in_str)[:19], fmt)
        end = datetime.strptime(str(check_out_str)[:19], fmt) if check_out_str else datetime.utcnow()
        mins = (end - ci).total_seconds() / 60
        if mins < 60:
            return f"{int(mins)}m"
        return f"{int(mins//60)}h {int(mins%60)}m"
    except Exception:
        return "—"


def format_dt(dt_str):
    try:
        return datetime.strptime(str(dt_str)[:19], "%Y-%m-%d %H:%M:%S").strftime("%d %b  %H:%M")
    except Exception:
        return str(dt_str) if dt_str else "—"


def is_overdue(category, check_in_str, estimated_minutes=None):
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        ci  = datetime.strptime(str(check_in_str)[:19], fmt)
        mins_passed = (datetime.utcnow() - ci).total_seconds() / 60
        
        # Rule 1: Delivery is strictly hardcoded to 20 minutes
        if category == "Delivery":
            return mins_passed > 20
            
        # Rule 2: If an estimated time was provided (Maintenance, Other), check it
        if estimated_minutes:
            return mins_passed > float(estimated_minutes)
            
        # Rule 3: Guests (or anyone without a timer) are never overdue
        return False
    except Exception:
        return False


# ── WEB ROUTES ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "guard_name" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        # Simple web login — username can be anything, password must match WEB_PASSWORD
        # For a production system you'd check against the guards table
        if password == WEB_PASSWORD and username:
            session["guard_name"] = username.title()
            session["username"]   = username
            return redirect(url_for("dashboard"))
        else:
            return render_template("login.html",
                                   error="Invalid credentials. Access denied.")

    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    raw_visits = get_active_visits_server()

    # Enrich each visit with display fields
    visits = []
    for v in raw_visits:
        v["entry_display"] = format_dt(v["check_in_time"])
        v["duration"]      = duration_str(v["check_in_time"])
        v["overdue"]       = is_overdue(v["category"], v["check_in_time"], v.get("estimated_minutes"))
        v["pax_ids"]       = v.get("pax_ids")  # Include the associated IDs
        visits.append(v)

    return render_template("dashboard.html", visits=visits)


@app.route("/checkin", methods=["POST"])
@login_required
def checkin():
    full_name    = request.form.get("full_name",    "").strip()
    national_id  = request.form.get("national_id",  "").strip()
    phone_number = request.form.get("phone_number", "").strip()
    vehicle_plate= request.form.get("vehicle_plate","").strip()
    category     = request.form.get("category",     "").strip()
    no_id        = request.form.get("no_id") == "on"
    host_pin     = request.form.get("host_pin",     "").strip()

    est_mins_raw = request.form.get("estimated_minutes", "").strip()
    # Convert to an integer if the user typed something, otherwise save as None
    estimated_minutes = int(est_mins_raw) if est_mins_raw.isdigit() else None


    multi_pax    = request.form.get("multi_pax") == "on"
    pax_count    = int(request.form.get("pax_count", 1)) if multi_pax else 1

    # Add this right after getting the variables in the /checkin route
    if not no_id and national_id and not national_id.isdigit():
        flash("❌ National ID must contain only numbers.", "error")
        return redirect(url_for("dashboard"))

    if phone_number and not phone_number.isdigit():
        flash("❌ Phone number must contain only numbers.", "error")
        return redirect(url_for("dashboard"))

    # --- NEW CODE: Extract the associated IDs ---
    extracted_ids = []
    if multi_pax:
        for i in range(1, pax_count + 1):
            assoc_id = request.form.get(f"associated_id_{i}", "").strip()
            if assoc_id:
                extracted_ids.append(assoc_id)
    
    # Join them into a comma-separated string (e.g. "12345678, 87654321")
    pax_ids_string = ", ".join(extracted_ids) if extracted_ids else None
    # --------------------------------------------

    # Basic validation
    if not full_name or not category:
        flash("Full name and category are required.", "error")
        return redirect(url_for("dashboard"))

    if not no_id and not national_id:
        flash("National ID is required (or tick 'Visitor has NO ID').", "error")
        return redirect(url_for("dashboard"))

    if no_id and not host_pin:
        flash("Host Secret PIN is required for Zero-Trust entry.", "error")
        return redirect(url_for("dashboard"))

    # Build the visit data
    visitor_uuid = str(uuid.uuid4())
    log_uuid     = str(uuid.uuid4())
    now_str      = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    data = {
        "log_uuid":          log_uuid,
        "visitor_uuid":      visitor_uuid,
        "full_name":         full_name,
        "national_id":       national_id if not no_id else None,
        "phone_number":      phone_number,
        "vehicle_plate":     vehicle_plate,
        "category":          category,
        "exception_flag":    no_id,
        "check_in_time":     now_str,
        "check_out_time":    None,
        "pax_count":         pax_count + 1 if multi_pax else 1,
        "pax_ids":           pax_ids_string,
        "estimated_minutes": estimated_minutes, # <-- NEW LINE ADDED HERE
        "guard_id":          None,
        "resident_id":       None,
    }

    category     = request.form.get("category",     "").strip()
    
    # --- NEW: Handle "Other" category substitution ---
    if category == "Other":
        custom_category = request.form.get("other_category", "").strip()
        if custom_category:
            category = custom_category.title()  # e.g., "Contractor"
    # -------------------------------------------------



    success = upsert_visit(data)

    if success:
        flash(f"✅ {full_name} checked in successfully.", "success")
    else:
        flash("❌ Check-in failed. Please try again.", "error")

    return redirect(url_for("dashboard"))


@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    log_uuid = request.form.get("log_uuid", "").strip()
    if not log_uuid:
        flash("Invalid checkout request.", "error")
        return redirect(url_for("dashboard"))

    success = web_checkout(log_uuid)
    if success:
        flash("✅ Visitor checked out successfully.", "success")
    else:
        flash("❌ Checkout failed — visitor may already be checked out.", "error")

    return redirect(url_for("dashboard"))


@app.route("/reports")
@login_required
def reports():
    category  = request.args.get("category", "").strip() or None
    date_from = request.args.get("date_from", "").strip() or None
    date_to   = request.args.get("date_to",   "").strip() or None

    raw_history = get_filtered_history(category, date_from, date_to)
    stats       = get_stats_server()

    history = []
    for r in raw_history:
        r["check_in_display"]  = format_dt(r["check_in_time"])
        r["check_out_display"] = format_dt(r["check_out_time"])
        r["duration"]          = duration_str(r["check_in_time"], r["check_out_time"])
        history.append(r)

    return render_template("reports.html",
                           history=history,
                           stats=stats,
                           request=request)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)