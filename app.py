"""
server/app.py — Flask Server with full web interface

CHANGES IN THIS VERSION:
  - TIMEZONE FIX: check_in_time is now stored as EAT (UTC+3) instead of UTC.
    Both datetime.utcnow() calls replaced with eat_now() helper.
    web_checkout() in server_db.py also updated to use NOW() AT TIME ZONE 'Africa/Nairobi'.
  - GUARD INFO: get_active_visits_server() now returns guard_name so the
    dashboard can show it in a detail popup when a row is clicked.
  - PAX POPUP: Associated Visitor IDs no longer shown as a column — clicking
    the Pax count badge opens a small modal instead.
  - TWO-WAY SYNC: /api/pull/visits endpoint added so the desktop SyncEngine
    can pull records that were created on the web dashboard.
"""

import os
import sys
import uuid
import hashlib
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, jsonify)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.routes import api_bp
from data.server_db import (
    init_server_db,
    get_active_visits_server,
    get_filtered_history,
    get_stats_server,
    web_checkout,
    upsert_visit,
    upsert_passenger,
    verify_guard_web,
    get_all_guards_server,
    add_guard_server,
    toggle_guard_server,
    reset_guard_password_server,
    get_all_residents_server,
    add_resident_server,
    update_resident_server,
    toggle_resident_server,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "vts-secret-key-change-in-production")
app.register_blueprint(api_bp)

# Initialise database tables on startup
try:
    init_server_db()
    print("[Startup] Database tables ready.")
except Exception as e:
    print(f"[Startup] DB init warning: {e}")


# ── TIMEZONE HELPER ────────────────────────────────────────────────────────
# East Africa Time = UTC + 3 hours.
# All check-in/out timestamps stored and displayed in EAT.

EAT = timezone(timedelta(hours=3))

def eat_now() -> str:
    """Returns current East Africa Time as a string: '2026-04-15 14:30:00'"""
    return datetime.now(EAT).strftime("%Y-%m-%d %H:%M:%S")


# ── DECORATORS ─────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "guard_name" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "guard_name" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            flash("⛔ Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


# ── HELPERS ────────────────────────────────────────────────────────────────

def duration_str(check_in_str, check_out_str=None):
    """Calculates duration between check-in and check-out (or now) in EAT."""
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        ci  = datetime.strptime(str(check_in_str)[:19], fmt)
        if check_out_str:
            end = datetime.strptime(str(check_out_str)[:19], fmt)
        else:
            # Compare against EAT now (no timezone object — naive comparison)
            end = datetime.now(EAT).replace(tzinfo=None)
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
    """
    Compares check-in time (stored as EAT) against current EAT.
    Both are naive datetimes (no tzinfo) so subtraction works directly.
    """
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        ci  = datetime.strptime(str(check_in_str)[:19], fmt)
        now_eat = datetime.now(EAT).replace(tzinfo=None)
        mins_passed = (now_eat - ci).total_seconds() / 60
        if category == "Delivery":
            return mins_passed > 20
        if estimated_minutes:
            return mins_passed > float(estimated_minutes)
        return False
    except Exception:
        return False


# ── AUTH ROUTES ────────────────────────────────────────────────────────────

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

        guard = verify_guard_web(username, password)
        if guard:
            session["guard_name"] = guard["full_name"]
            session["username"]   = guard["username"]
            session["guard_id"]   = guard["guard_id"]
            session["role"]       = guard.get("role", "guard")
            return redirect(url_for("dashboard"))
        else:
            return render_template("login.html",
                                   error="Invalid credentials. Access denied.")

    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── DASHBOARD ──────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    raw_visits = get_active_visits_server()

    visits = []
    for v in raw_visits:
        v["entry_display"] = format_dt(v["check_in_time"])
        v["duration"]      = duration_str(v["check_in_time"])
        v["overdue"]       = is_overdue(
            v["category"], v["check_in_time"], v.get("estimated_minutes")
        )
        visits.append(v)

    return render_template("dashboard.html", visits=visits)


# ── CHECK-IN ───────────────────────────────────────────────────────────────

@app.route("/checkin", methods=["POST"])
@login_required
def checkin():
    full_name     = request.form.get("full_name",     "").strip()
    national_id   = request.form.get("national_id",   "").strip()
    phone_number  = request.form.get("phone_number",  "").strip()
    vehicle_plate = request.form.get("vehicle_plate", "").strip()
    no_id         = request.form.get("no_id") == "on"
    host_pin      = request.form.get("host_pin",      "").strip()
    multi_pax     = request.form.get("multi_pax") == "on"
    pax_count_extra = int(request.form.get("pax_count", 1)) if multi_pax else 0

    # Resolve category
    raw_category = request.form.get("category", "").strip()
    if raw_category == "Other":
        custom   = request.form.get("other_category", "").strip()
        category = custom.title() if custom else "Other"
    else:
        category = raw_category

    # Estimated minutes
    est_raw = request.form.get("estimated_minutes", "").strip()
    if category == "Delivery":
        estimated_minutes = 20
    elif est_raw.isdigit():
        estimated_minutes = int(est_raw)
    else:
        estimated_minutes = None

    # Validation
    if not full_name or not category:
        flash("❌ Full name and category are required.", "error")
        return redirect(url_for("dashboard"))

    if not no_id and not national_id:
        flash("❌ National ID is required (or tick 'Visitor has NO ID').", "error")
        return redirect(url_for("dashboard"))

    if not no_id and national_id and not national_id.isdigit():
        flash("❌ National ID must contain only numbers.", "error")
        return redirect(url_for("dashboard"))

    if phone_number and not phone_number.isdigit():
        flash("❌ Phone number must contain only numbers.", "error")
        return redirect(url_for("dashboard"))

    if no_id and not host_pin:
        flash("❌ Host Secret PIN is required for Zero-Trust entry.", "error")
        return redirect(url_for("dashboard"))

    # Collect associated visitor IDs
    passenger_ids = []
    if multi_pax:
        for i in range(1, pax_count_extra + 1):
            pid = request.form.get(f"associated_id_{i}", "").strip()
            if pid:
                passenger_ids.append(pid)

    # Build UUIDs and timestamp — NOW STORED IN EAT, NOT UTC
    visitor_uuid = str(uuid.uuid4())
    log_uuid     = str(uuid.uuid4())
    now_str      = eat_now()          # ← FIXED: was datetime.utcnow()
    total_pax    = 1 + pax_count_extra if multi_pax else 1

    data = {
        "log_uuid":          log_uuid,
        "visitor_uuid":      visitor_uuid,
        "full_name":         full_name,
        "national_id":       national_id if not no_id else None,
        "phone_number":      phone_number or None,
        "vehicle_plate":     vehicle_plate or None,
        "category":          category,
        "exception_flag":    no_id,
        "check_in_time":     now_str,
        "check_out_time":    None,
        "pax_count":         total_pax,
        "estimated_minutes": estimated_minutes,
        "guard_id":          session.get("guard_id"),
        "resident_id":       None,
    }

    success = upsert_visit(data)

    if success and passenger_ids:
        for pid in passenger_ids:
            upsert_passenger(log_uuid, pid)

    if success:
        flash(f"✅ {full_name} checked in successfully.", "success")
    else:
        flash("❌ Check-in failed. Please try again.", "error")

    return redirect(url_for("dashboard"))


# ── CHECK-OUT ──────────────────────────────────────────────────────────────

@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    log_uuid = request.form.get("log_uuid", "").strip()
    if not log_uuid:
        flash("❌ Invalid checkout request.", "error")
        return redirect(url_for("dashboard"))

    success = web_checkout(log_uuid)
    if success:
        flash("✅ Visitor checked out successfully.", "success")
    else:
        flash("❌ Checkout failed — visitor may already be checked out.", "error")

    return redirect(url_for("dashboard"))


# ── REPORTS ────────────────────────────────────────────────────────────────

@app.route("/reports")
@login_required
def reports():
    category  = request.args.get("category",  "").strip() or None
    date_from = request.args.get("date_from", "").strip() or None
    date_to   = request.args.get("date_to",   "").strip() or None

    raw_history = get_filtered_history(category, date_from, date_to)
    stats       = get_stats_server()

    history = []
    for r in raw_history:
        r["check_in_display"]  = format_dt(r["check_in_time"])
        r["check_out_display"] = format_dt(r["check_out_time"])
        r["duration"]          = duration_str(r["check_in_time"], r["check_out_time"])
        r["was_overdue"]       = bool(r.get("was_overdue", False))
        history.append(r)

    return render_template("reports.html",
                           history=history,
                           stats=stats,
                           request=request)


# ── MANAGE GUARDS (admin only) ─────────────────────────────────────────────

@app.route("/manage-guards")
@admin_required
def manage_guards():
    guards = get_all_guards_server()
    return render_template("manage_guards.html", guards=guards)


@app.route("/manage-guards/add", methods=["POST"])
@admin_required
def add_guard():
    username  = request.form.get("username",  "").strip()
    password  = request.form.get("password",  "").strip()
    full_name = request.form.get("full_name", "").strip()
    role      = request.form.get("role",      "guard").strip()

    if not username or not password or not full_name:
        flash("❌ All fields are required.", "error")
        return redirect(url_for("manage_guards"))
    if len(password) < 6:
        flash("❌ Password must be at least 6 characters.", "error")
        return redirect(url_for("manage_guards"))

    success = add_guard_server(username, password, full_name, role)
    if success:
        flash(f"✅ Guard '{username}' added successfully.", "success")
    else:
        flash(f"❌ Username '{username}' already exists.", "error")
    return redirect(url_for("manage_guards"))


@app.route("/manage-guards/toggle/<int:guard_id>", methods=["POST"])
@admin_required
def toggle_guard(guard_id):
    new_state = toggle_guard_server(guard_id)
    flash(f"✅ Guard {'activated' if new_state else 'deactivated'}.", "success")
    return redirect(url_for("manage_guards"))


@app.route("/manage-guards/reset-password/<int:guard_id>", methods=["POST"])
@admin_required
def reset_guard_password(guard_id):
    new_pw = request.form.get("new_password", "").strip()
    if len(new_pw) < 6:
        flash("❌ Password must be at least 6 characters.", "error")
        return redirect(url_for("manage_guards"))
    success = reset_guard_password_server(guard_id, new_pw)
    if success:
        flash("✅ Password updated.", "success")
    else:
        flash("❌ Failed to update password.", "error")
    return redirect(url_for("manage_guards"))


# ── MANAGE RESIDENTS (admin only) ──────────────────────────────────────────

@app.route("/manage-residents")
@admin_required
def manage_residents():
    residents = get_all_residents_server()
    return render_template("manage_residents.html", residents=residents)


@app.route("/manage-residents/add", methods=["POST"])
@admin_required
def add_resident():
    full_name   = request.form.get("full_name",   "").strip()
    unit_number = request.form.get("unit_number", "").strip()
    host_pin    = request.form.get("host_pin",    "").strip()
    phone       = request.form.get("phone",       "").strip()

    if not full_name or not unit_number or not host_pin:
        flash("❌ Full name, unit, and PIN are required.", "error")
        return redirect(url_for("manage_residents"))

    success = add_resident_server(full_name, unit_number, host_pin, phone)
    if success:
        flash(f"✅ Resident '{full_name}' added.", "success")
    else:
        flash(f"❌ PIN '{host_pin}' already exists. Choose a different PIN.", "error")
    return redirect(url_for("manage_residents"))


@app.route("/manage-residents/toggle/<int:resident_id>", methods=["POST"])
@admin_required
def toggle_resident(resident_id):
    new_state = toggle_resident_server(resident_id)
    flash(f"✅ Resident {'activated' if new_state else 'deactivated'}.", "success")
    return redirect(url_for("manage_residents"))


@app.route("/manage-residents/edit/<int:resident_id>", methods=["POST"])
@admin_required
def edit_resident(resident_id):
    full_name   = request.form.get("full_name",   "").strip()
    unit_number = request.form.get("unit_number", "").strip()
    phone       = request.form.get("phone",       "").strip()

    if not full_name or not unit_number:
        flash("❌ Full name and unit are required.", "error")
        return redirect(url_for("manage_residents"))

    success = update_resident_server(resident_id, full_name, unit_number, phone)
    if success:
        flash("✅ Resident updated.", "success")
    else:
        flash("❌ Update failed.", "error")
    return redirect(url_for("manage_residents"))


# ── STARTUP ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)