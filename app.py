"""
server/app.py — Standalone Flask web app (v3)

NEW IN v3:
  - Reason for visit on check-in (free-text)
  - Blacklist check on check-in (admin can override)
  - Autofill API for repeat visitors
  - Audit log writes on every important action + admin viewer page
  - Per-host type (office/residential), with optional host_email
  - Email notification to host on check-in (Gmail SMTP, opt-in via env vars)
  - CSV export for reports
  - Blacklist admin page
"""

import csv
import io
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from functools import wraps

EAT = timezone(timedelta(hours=3))


def now_eat() -> str:
    return datetime.now(EAT).strftime("%Y-%m-%d %H:%M:%S")


from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    jsonify, Response,
)

# ── Local package resolution ─────────────────────────────────────────────
# Railway deploys to /app so __file__ == /app/app.py  and data/ is /app/data/
# Locally __file__ may be /something/server/app.py    and data/ is /something/server/data/
# In both cases os.path.dirname(__file__) is correct — we just insert it
# explicitly so Python always finds the data/ and api/ packages.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from api.routes import api_bp  # noqa: E402
from data.server_db import (  # noqa: E402
    init_server_db,
    get_active_visits_server, get_filtered_history, get_stats_server,
    get_active_units, web_checkout, upsert_visit, upsert_passenger,
    verify_guard_web, get_visit_for_audit,
    get_all_guards_server, add_guard_server, toggle_guard_server,
    reset_guard_password_server,
    get_all_residents_server, get_active_hosts_for_dropdown,
    add_resident_server, update_resident_server, toggle_resident_server,
    record_audit, get_audit_log,
    check_blacklist, add_blacklist, remove_blacklist, get_all_blacklist,
    find_visitor_by_national_id,
    get_host_by_unit,
    clear_all_hosts,
    clear_all_visits,
)
# Load email_service safely — if the import fails for any reason,
# replace send_host_notification with a no-op so the app still boots.
try:
    from data.email_service import send_host_notification
except ImportError:
    def send_host_notification(*args, **kwargs):
        print("[Email] email_service not found — notifications disabled.")
        return False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "vts-secret-key-change-in-production")
app.register_blueprint(api_bp)

try:
    init_server_db()
    print("[Startup] Database tables ready.")
except Exception as e:
    print(f"[Startup] DB init warning: {e}")


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
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


# ── HELPERS ────────────────────────────────────────────────────────────────

def duration_str(check_in_str, check_out_str=None) -> str:
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        ci  = datetime.strptime(str(check_in_str)[:19], fmt)
        end = (datetime.strptime(str(check_out_str)[:19], fmt)
               if check_out_str
               else datetime.now(EAT).replace(tzinfo=None))
        mins = (end - ci).total_seconds() / 60
        if mins < 60:
            return f"{int(mins)}m"
        return f"{int(mins // 60)}h {int(mins % 60)}m"
    except Exception:
        return "—"


def format_dt(dt_str) -> str:
    try:
        return datetime.strptime(
            str(dt_str)[:19], "%Y-%m-%d %H:%M:%S"
        ).strftime("%d %b  %H:%M")
    except Exception:
        return str(dt_str) if dt_str else "—"


def is_overdue(category, check_in_str, estimated_minutes=None) -> bool:
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        ci  = datetime.strptime(str(check_in_str)[:19], fmt)
        mins_passed = (
            datetime.now(EAT).replace(tzinfo=None) - ci
        ).total_seconds() / 60
        if category == "Delivery":
            return mins_passed > 20
        if estimated_minutes:
            return mins_passed > float(estimated_minutes)
        return False
    except Exception:
        return False


def _audit(action, target=None, details=None):
    """Convenience wrapper — pulls actor info from session + IP from request."""
    try:
        record_audit(
            actor_guard_id=session.get("guard_id"),
            actor_name=session.get("guard_name", "anonymous"),
            action=action,
            target=target,
            details=details,
            ip_address=request.headers.get(
                "X-Forwarded-For", request.remote_addr
            ),
        )
    except Exception as e:
        print(f"[Audit] _audit wrapper error: {e}")


# ── AUTH ──────────────────────────────────────────────────────────────────

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
            _audit("LOGIN_SUCCESS", target=username)
            return redirect(url_for("dashboard"))

        # Audit failed attempt with username, no actor (no session yet)
        try:
            record_audit(
                actor_guard_id=None, actor_name="anonymous",
                action="LOGIN_FAILED", target=username,
                ip_address=request.headers.get(
                    "X-Forwarded-For", request.remote_addr
                ),
            )
        except Exception:
            pass

        return render_template(
            "login.html",
            error="Invalid credentials. Access denied.",
        )

    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    _audit("LOGOUT")
    session.clear()
    return redirect(url_for("login"))


# ── DASHBOARD ─────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    unit_filter = request.args.get("unit", "").strip() or None
    raw_visits  = get_active_visits_server(host_unit_filter=unit_filter)
    hosts       = get_active_hosts_for_dropdown()
    units       = get_active_units()

    visits = []
    for v in raw_visits:
        v["entry_display"] = format_dt(v["check_in_time"])
        v["duration"]      = duration_str(v["check_in_time"])
        v["overdue"]       = is_overdue(
            v["category"], v["check_in_time"], v.get("estimated_minutes")
        )
        visits.append(v)

    return render_template(
        "dashboard.html",
        visits=visits, hosts=hosts, units=units,
        unit_filter=unit_filter or "",
    )


# ── AUTOFILL API ──────────────────────────────────────────────────────────

@app.route("/api/lookup-visitor")
@login_required
def lookup_visitor():
    nid = request.args.get("nid", "").strip()
    if not nid or not nid.isdigit() or len(nid) < 5:
        return jsonify({"found": False})

    visitor = find_visitor_by_national_id(nid)
    flagged = check_blacklist(nid)

    payload = {"found": False, "blacklisted": False}
    if visitor:
        payload.update({
            "found":         True,
            "full_name":     visitor.get("full_name", ""),
            "phone_number":  visitor.get("phone_number", "") or "",
            "vehicle_plate": visitor.get("vehicle_plate", "") or "",
        })
    if flagged:
        payload.update({
            "blacklisted":  True,
            "blacklist_reason": flagged.get("reason", ""),
        })
    return jsonify(payload)


# ── CHECK-IN ──────────────────────────────────────────────────────────────

@app.route("/checkin", methods=["POST"])
@login_required
def checkin():
    full_name       = request.form.get("full_name",     "").strip()
    national_id     = request.form.get("national_id",   "").strip()
    phone_number    = request.form.get("phone_number",  "").strip()
    vehicle_plate   = request.form.get("vehicle_plate", "").strip()
    no_id           = request.form.get("no_id") == "on"
    host_pin        = request.form.get("host_pin",      "").strip()
    host_unit       = request.form.get("host_unit",     "").strip()
    reason          = request.form.get("reason",        "").strip()
    multi_pax       = request.form.get("multi_pax") == "on"
    pax_count_extra = int(request.form.get("pax_count", 1)) if multi_pax else 0
    blacklist_override = request.form.get("blacklist_override") == "on"

    raw_category = request.form.get("category", "").strip()
    if raw_category == "Other":
        custom = request.form.get("other_category", "").strip()
        category = custom.title() if custom else "Other"
    else:
        category = raw_category

    est_raw = request.form.get("estimated_minutes", "").strip()
    if category == "Delivery":
        estimated_minutes = 20
    elif est_raw.isdigit():
        estimated_minutes = int(est_raw)
    else:
        estimated_minutes = None

    # Validation
    if not full_name or not category:
        flash("Full name and category are required.", "error")
        return redirect(url_for("dashboard"))
    if not host_unit:
        flash("Please select a unit or office for this visit.", "error")
        return redirect(url_for("dashboard"))
    if not reason:
        flash("Reason for visit is required.", "error")
        return redirect(url_for("dashboard"))
    if not no_id and not national_id:
        flash("National ID is required (or tick 'Visitor has NO ID').", "error")
        return redirect(url_for("dashboard"))
    if not no_id and national_id and not national_id.isdigit():
        flash("National ID must contain only numbers.", "error")
        return redirect(url_for("dashboard"))
    if phone_number and not phone_number.isdigit():
        flash("Phone number must contain only numbers.", "error")
        return redirect(url_for("dashboard"))
    if no_id and not host_pin:
        flash("Host secret PIN is required for zero-trust entry.", "error")
        return redirect(url_for("dashboard"))

    # BLACKLIST CHECK
    if not no_id and national_id:
        flagged = check_blacklist(national_id)
        if flagged and not blacklist_override:
            flash(
                f"⛔ BLOCKED: This visitor is on the blacklist. "
                f"Reason: {flagged['reason']}. "
                f"Only an admin can override this by re-submitting with the override box ticked.",
                "error",
            )
            _audit(
                "BLACKLIST_BLOCKED",
                target=f"NID:{national_id}",
                details=f"Visitor {full_name} blocked. Reason: {flagged['reason']}",
            )
            return redirect(url_for("dashboard"))
        if flagged and blacklist_override:
            if session.get("role") != "admin":
                flash(
                    "Only admins can override the blacklist. Visitor blocked.",
                    "error",
                )
                _audit(
                    "BLACKLIST_OVERRIDE_DENIED",
                    target=f"NID:{national_id}",
                    details=f"Non-admin tried to override block on {full_name}",
                )
                return redirect(url_for("dashboard"))
            _audit(
                "BLACKLIST_OVERRIDE",
                target=f"NID:{national_id}",
                details=f"Admin allowed {full_name} despite blacklist. Reason: {flagged['reason']}",
            )

    passengers = []  # list of (national_id, full_name) tuples
    if multi_pax:
        for i in range(1, pax_count_extra + 1):
            pid  = request.form.get(f"associated_id_{i}",   "").strip()
            pname= request.form.get(f"associated_name_{i}", "").strip()
            if pid:
                passengers.append((pid, pname or None))

    visitor_uuid = str(uuid.uuid4())
    log_uuid     = str(uuid.uuid4())
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
        "check_in_time":     now_eat(),
        "check_out_time":    None,
        "pax_count":         total_pax,
        "estimated_minutes": estimated_minutes,
        "guard_id":          session.get("guard_id"),
        "resident_id":       None,
        "host_unit":         host_unit or None,
        "reason_for_visit":  reason or None,
    }

    success = upsert_visit(data)
    if success and passengers:
        for pid, pname in passengers:
            upsert_passenger(log_uuid, pid, pname)

    if success:
        flash(f"{full_name} checked in successfully.", "success")
        _audit(
            "CHECK_IN",
            target=log_uuid,
            details=f"{full_name} ({category}) at {host_unit}, "
                    f"pax={total_pax}, reason={reason or '—'}",
        )

        # Email notification (best-effort, never blocks the flow)
        try:
            host_record = get_host_by_unit(host_unit)
            if host_record and host_record.get("host_email"):
                sent = send_host_notification(
                    host_email=host_record["host_email"],
                    host_name=host_record["full_name"],
                    visitor_name=full_name,
                    visitor_category=category,
                    unit=host_unit,
                    check_in_time=data["check_in_time"],
                    reason=reason,
                )
                if sent:
                    _audit(
                        "EMAIL_SENT", target=host_record["host_email"],
                        details=f"Host notification for visitor {full_name}",
                    )
        except Exception as e:
            print(f"[Email] notification block failed: {e}")
    else:
        flash("Check-in failed. Please try again.", "error")

    return redirect(url_for("dashboard"))


# ── CHECK-OUT ─────────────────────────────────────────────────────────────

@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    log_uuid = request.form.get("log_uuid", "").strip()
    if not log_uuid:
        flash("Invalid checkout request.", "error")
        return redirect(url_for("dashboard"))

    visit_meta = get_visit_for_audit(log_uuid)
    success = web_checkout(log_uuid, guard_id=session.get("guard_id"))
    if success:
        flash("Visitor checked out successfully.", "success")
        _audit(
            "CHECK_OUT", target=log_uuid,
            details=f"{visit_meta['full_name']}" if visit_meta else log_uuid,
        )
    else:
        flash("Checkout failed — visitor may already be checked out.", "error")

    return redirect(url_for("dashboard"))


# ── REPORTS ────────────────────────────────────────────────────────────────

def _gather_report_filters():
    return {
        "category":  request.args.get("category",  "").strip() or None,
        "date_from": request.args.get("date_from", "").strip() or None,
        "date_to":   request.args.get("date_to",   "").strip() or None,
        "time_from": request.args.get("time_from", "").strip() or None,
        "time_to":   request.args.get("time_to",   "").strip() or None,
        "host_unit": request.args.get("host_unit", "").strip() or None,
    }


@app.route("/reports")
@login_required
def reports():
    f = _gather_report_filters()
    raw_history = get_filtered_history(**f)
    stats = get_stats_server()
    units = get_active_units()

    history = []
    for r in raw_history:
        r["check_in_display"]  = format_dt(r["check_in_time"])
        r["check_out_display"] = format_dt(r["check_out_time"])
        r["duration"]          = duration_str(r["check_in_time"], r["check_out_time"])
        r["was_overdue"]       = bool(r.get("was_overdue", False))
        history.append(r)

    return render_template(
        "reports.html", history=history, stats=stats, units=units,
        request=request,
    )


@app.route("/reports/export.csv")
@login_required
def reports_export_csv():
    f = _gather_report_filters()
    raw_history = get_filtered_history(**f)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Name", "National ID", "Category", "Reason", "Unit",
        "Pax", "Associated IDs",
        "Check-in", "Check-out", "Duration (min)",
        "Checked in by", "Checked out by",
        "Was overdue", "No-ID entry",
    ])

    for r in raw_history:
        # Compute duration in minutes for spreadsheet sorting
        try:
            ci = datetime.strptime(str(r["check_in_time"])[:19],  "%Y-%m-%d %H:%M:%S")
            co = datetime.strptime(str(r["check_out_time"])[:19], "%Y-%m-%d %H:%M:%S")
            dur_min = int((co - ci).total_seconds() / 60)
        except Exception:
            dur_min = ""

        writer.writerow([
            r.get("full_name", ""),
            r.get("national_id", "") or "",
            r.get("category", ""),
            r.get("reason_for_visit", "") or "",
            r.get("host_unit", "") or "",
            r.get("pax_count", 1),
            (r.get("pax_ids") or "").replace("|", ";") if r.get("pax_ids") != "—" else "",
            r.get("check_in_time", ""),
            r.get("check_out_time", ""),
            dur_min,
            r.get("checkin_guard_name", "") or "",
            r.get("checkout_guard_name", "") or "",
            "Yes" if r.get("was_overdue") else "No",
            "Yes" if r.get("exception_flag") else "No",
        ])

    _audit("EXPORT_CSV", details=f"Exported {len(raw_history)} record(s)")

    csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM for Excel
    filename  = f"vts-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── MANAGE GUARDS ─────────────────────────────────────────────────────────

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
        flash("All fields are required.", "error")
        return redirect(url_for("manage_guards"))
    if len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("manage_guards"))

    success = add_guard_server(username, password, full_name, role)
    if success:
        flash(f"Guard '{username}' added successfully.", "success")
        _audit("GUARD_ADD", target=username, details=f"role={role}")
    else:
        flash(f"Username '{username}' already exists.", "error")
    return redirect(url_for("manage_guards"))


@app.route("/manage-guards/toggle/<int:guard_id>", methods=["POST"])
@admin_required
def toggle_guard(guard_id):
    result = toggle_guard_server(
        guard_id, requesting_guard_id=session.get("guard_id")
    )
    if result == "self":
        flash(
            "You cannot disable or re-enable your own account.",
            "error",
        )
        _audit("GUARD_SELF_TOGGLE_BLOCKED", target=str(guard_id))
    elif result is None:
        flash("Toggle failed — please try again.", "error")
    else:
        flash(f"Guard {'activated' if result else 'deactivated'}.", "success")
        _audit(
            "GUARD_TOGGLE", target=str(guard_id),
            details=f"new state: {'active' if result else 'inactive'}",
        )
    return redirect(url_for("manage_guards"))


@app.route("/manage-guards/reset-password/<int:guard_id>", methods=["POST"])
@admin_required
def reset_guard_password(guard_id):
    new_pw = request.form.get("new_password", "").strip()
    if len(new_pw) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("manage_guards"))
    success = reset_guard_password_server(guard_id, new_pw)
    if success:
        flash("Password updated.", "success")
        _audit("GUARD_PASSWORD_RESET", target=str(guard_id))
    else:
        flash("Failed to update password.", "error")
    return redirect(url_for("manage_guards"))


# ── MANAGE HOSTS ──────────────────────────────────────────────────────────

@app.route("/manage-hosts")
@admin_required
def manage_hosts():
    unit_filter = request.args.get("unit", "").strip() or None
    type_filter = request.args.get("type", "").strip() or None
    all_hosts   = get_all_residents_server()

    hosts = all_hosts
    if unit_filter:
        hosts = [h for h in hosts
                 if unit_filter.lower() in (h["unit_number"] or "").lower()]
    if type_filter in ("office", "residential"):
        hosts = [h for h in hosts if h.get("host_type") == type_filter]

    units = sorted({h["unit_number"] for h in all_hosts if h["unit_number"]})
    return render_template(
        "manage_hosts.html", hosts=hosts, units=units,
        unit_filter=unit_filter or "", type_filter=type_filter or "",
    )


@app.route("/manage-hosts/add", methods=["POST"])
@admin_required
def add_host():
    full_name   = request.form.get("full_name",   "").strip()
    unit_number = request.form.get("unit_number", "").strip()
    host_pin    = request.form.get("host_pin",    "").strip()
    phone       = request.form.get("phone",       "").strip()
    host_type   = request.form.get("host_type",   "residential").strip()
    host_email  = request.form.get("host_email",  "").strip()

    if not full_name or not unit_number or not host_pin:
        flash("Full name, unit, and PIN are required.", "error")
        return redirect(url_for("manage_hosts"))

    if host_email and "@" not in host_email:
        flash("Email looks invalid. Please enter a valid email or leave it blank.", "error")
        return redirect(url_for("manage_hosts"))

    success = add_resident_server(
        full_name, unit_number, host_pin, phone, host_type, host_email,
    )
    if success:
        flash(f"Host '{full_name}' added.", "success")
        _audit(
            "HOST_ADD", target=unit_number,
            details=f"{full_name} ({host_type}), email: {host_email or '—'}",
        )
    else:
        flash(f"PIN '{host_pin}' already exists. Choose a different PIN.", "error")
    return redirect(url_for("manage_hosts"))


@app.route("/manage-hosts/toggle/<int:host_id>", methods=["POST"])
@admin_required
def toggle_host(host_id):
    new_state = toggle_resident_server(host_id)
    flash(f"Host {'activated' if new_state else 'deactivated'}.", "success")
    _audit(
        "HOST_TOGGLE", target=str(host_id),
        details=f"new state: {'active' if new_state else 'inactive'}",
    )
    return redirect(url_for("manage_hosts"))


@app.route("/manage-hosts/edit/<int:host_id>", methods=["POST"])
@admin_required
def edit_host(host_id):
    full_name   = request.form.get("full_name",   "").strip()
    unit_number = request.form.get("unit_number", "").strip()
    phone       = request.form.get("phone",       "").strip()
    host_type   = request.form.get("host_type",   "").strip() or None
    host_email  = request.form.get("host_email",  "").strip()

    if not full_name or not unit_number:
        flash("Full name and unit are required.", "error")
        return redirect(url_for("manage_hosts"))

    success = update_resident_server(
        host_id, full_name, unit_number, phone, host_type, host_email,
    )
    if success:
        flash("Host updated.", "success")
        _audit("HOST_EDIT", target=str(host_id),
               details=f"{full_name} @ {unit_number}")
    else:
        flash("Update failed.", "error")
    return redirect(url_for("manage_hosts"))


# Backwards-compat
@app.route("/manage-residents")
@admin_required
def manage_residents_legacy():
    return redirect(url_for("manage_hosts"))


# ── BLACKLIST ─────────────────────────────────────────────────────────────

@app.route("/blacklist")
@admin_required
def blacklist():
    entries = get_all_blacklist()
    return render_template("blacklist.html", entries=entries)


@app.route("/blacklist/add", methods=["POST"])
@admin_required
def blacklist_add():
    national_id = request.form.get("national_id", "").strip()
    full_name   = request.form.get("full_name",   "").strip()
    reason      = request.form.get("reason",      "").strip()

    if not national_id or not reason:
        flash("National ID and reason are required.", "error")
        return redirect(url_for("blacklist"))
    if not national_id.isdigit():
        flash("National ID must be numeric only.", "error")
        return redirect(url_for("blacklist"))

    success = add_blacklist(
        national_id, full_name, reason, session.get("guard_id"),
    )
    if success:
        flash(f"NID {national_id} added to blacklist.", "success")
        _audit(
            "BLACKLIST_ADD", target=f"NID:{national_id}",
            details=f"{full_name or '—'} | reason: {reason}",
        )
    else:
        flash("Failed to add to blacklist.", "error")
    return redirect(url_for("blacklist"))


@app.route("/blacklist/remove/<int:bl_id>", methods=["POST"])
@admin_required
def blacklist_remove(bl_id):
    success = remove_blacklist(bl_id)
    if success:
        flash("Removed from blacklist.", "success")
        _audit("BLACKLIST_REMOVE", target=str(bl_id))
    else:
        flash("Failed to remove.", "error")
    return redirect(url_for("blacklist"))


# ── AUDIT LOG ─────────────────────────────────────────────────────────────

@app.route("/audit-log")
@admin_required
def audit_log():
    action_filter = request.args.get("action", "").strip() or None
    actor_filter  = request.args.get("actor",  "").strip() or None
    entries       = get_audit_log(
        limit=300, action_filter=action_filter, actor_filter=actor_filter,
    )
    return render_template(
        "audit_log.html", entries=entries,
        action_filter=action_filter or "",
        actor_filter=actor_filter or "",
    )





@app.route("/admin/test-email")
@admin_required
def test_email():
    import os
    enabled    = os.environ.get("EMAIL_ENABLED", "").lower() == "true"
    smtp_user  = os.environ.get("SMTP_USER", "")
    smtp_pass  = os.environ.get("SMTP_PASSWORD", "").replace(" ", "")
    from_name  = os.environ.get("SMTP_FROM_NAME", "VTS")

    status_box = (
        "<div style='background:#FFF3CD;border:1px solid #FBBF24;border-radius:8px;"
        "padding:14px;margin-bottom:16px;font-size:13px;'>"
        "<b>⚠ Email notifications are currently DISABLED</b><br><br>"
        "The feature is fully built and ready. It is disabled because Railway's free tier "
        "blocks all outbound network connections required to send email.<br><br>"
        "To enable when on a paid host or after migrating:<br>"
        "1. Add env var <code>EMAIL_ENABLED=true</code><br>"
        "2. Ensure <code>SMTP_USER</code> and <code>SMTP_PASSWORD</code> (16-char Gmail App Password) are set<br>"
        "3. Add an email address to each host in Manage Hosts"
        "</div>"
    ) if not enabled else (
        "<div style='background:#D1FAE5;border:1px solid #34D399;border-radius:8px;"
        "padding:14px;margin-bottom:16px;font-size:13px;'>"
        "<b>✅ Email notifications are ENABLED</b><br>"
        f"Sending from: {smtp_user}"
        "</div>"
    )

    from data.server_db import get_connection
    host_rows = ""
    try:
        conn = get_connection(); cur = conn.cursor()
        cur.execute("SELECT full_name, unit_number, host_email FROM residents WHERE is_active=TRUE ORDER BY unit_number")
        for r in cur.fetchall():
            color = "green" if r["host_email"] else "#999"
            val   = r["host_email"] or "—"
            host_rows += (f"<tr><td>{r['full_name']}</td><td>{r['unit_number']}</td>"
                         f"<td style='color:{color}'>{val}</td></tr>")
        cur.close(); conn.close()
    except Exception as e:
        host_rows = f"<tr><td colspan=3>DB error: {e}</td></tr>"

    return (
        "<!DOCTYPE html><html><head><style>"
        "body{font-family:sans-serif;padding:28px;max-width:680px;}"
        "table{border-collapse:collapse;width:100%;margin-top:10px;}"
        "th,td{border:1px solid #ddd;padding:8px;font-size:13px;text-align:left;}"
        "th{background:#f5f5f5;}code{background:#f0f0f0;padding:2px 6px;border-radius:4px;}"
        ".btn{display:inline-block;padding:10px 18px;background:#008564;color:#fff;"
        "text-decoration:none;border-radius:6px;margin-top:16px;}"
        "</style></head><body>"
        "<h2>📧 Email Notification Status</h2>"
        + status_box +
        "<h3>Host notification emails</h3>"
        "<table><tr><th>Host</th><th>Unit</th><th>Email on file</th></tr>"
        + host_rows +
        "</table>"
        "<a href='/dashboard' class='btn'>← Back</a>"
        "</body></html>"
    )


@app.route("/admin/db-status")
@admin_required
def db_status():
    """Shows raw DB state — use this to diagnose PIN issues."""
    from data.server_db import get_connection
    try:
        conn = get_connection()
        cur  = conn.cursor()

        cur.execute("SELECT COUNT(*) AS cnt FROM residents")
        host_count = cur.fetchone()["cnt"]

        cur.execute("SELECT resident_id, full_name, unit_number, host_pin, is_active FROM residents ORDER BY resident_id")
        hosts = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) AS cnt FROM visit_logs")
        visit_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM visitors")
        visitor_count = cur.fetchone()["cnt"]

        cur.close()
        conn.close()

        rows = "".join(
            f"<tr><td>{h['resident_id']}</td><td>{h['full_name']}</td>"
            f"<td>{h['unit_number']}</td>"
            f"<td style='font-family:monospace'>[{repr(h['host_pin'])}]</td>"
            f"<td>{'Active' if h['is_active'] else 'Inactive'}</td></tr>"
            for h in hosts
        )

        html = f"""<!DOCTYPE html><html><head>
        <style>body{{font-family:sans-serif;padding:24px;}}
        table{{border-collapse:collapse;width:100%;}}
        th,td{{border:1px solid #ccc;padding:8px;text-align:left;}}
        th{{background:#f0f0f0;}}
        .btn{{display:inline-block;padding:10px 18px;background:#008564;color:#fff;
              text-decoration:none;border-radius:6px;margin:6px 4px;border:none;cursor:pointer;font-size:14px;}}
        .btn-red{{background:#a32d2d;}}
        </style></head><body>
        <h2>🛠 DB Status</h2>
        <p>Hosts: <strong>{host_count}</strong> &nbsp;|&nbsp;
           Visits: <strong>{visit_count}</strong> &nbsp;|&nbsp;
           Visitors: <strong>{visitor_count}</strong></p>
        <h3>Residents table (raw)</h3>
        <table><tr><th>ID</th><th>Name</th><th>Unit</th><th>PIN (exact)</th><th>Active</th></tr>
        {rows if rows else '<tr><td colspan=5>Empty</td></tr>'}
        </table>
        <br>
        <form action="/admin/db-purge-bad-pins" method="POST" style="display:inline;">
            <button class="btn" onclick="return confirm('Remove rows with blank/null PINs?')">
                🗑 Remove blank/invalid PIN rows
            </button>
        </form>
        <form action="/admin/fix-pin-constraint" method="POST" style="display:inline;">
            <button class="btn" style="background:#185FA5;" onclick="return confirm('Rebuild PIN unique index?')">
                🔧 Rebuild PIN constraint
            </button>
        </form>
        <form action="/admin/reset-hosts" method="POST" style="display:inline;">
            <button class="btn btn-red" onclick="return confirm('Wipe ALL hosts?')">
                🗑 Wipe ALL hosts
            </button>
        </form>
        <form action="/admin/full-reset-and-seed" method="POST" style="display:inline;">
            <button class="btn" style="background:#3B6D11;"
                    onclick="return confirm('Wipe ALL data and seed demo data? Cannot be undone.')">
                🌱 Full reset + seed demo data
            </button>
        </form>
        <form action=\"/admin/reset-visits\" method=\"POST\" style=\"display:inline;\"><button class=\"btn\" style=\"background:#A32D2D;\" onclick=\"return confirm('Clear ALL visit data?')\">🗑 Clear all visit data</button></form>        <br><br><a href=\"/manage-hosts\" class=\"btn\">← Back to Hosts</a>
        </body></html>"""
        return html
    except Exception as e:
        return f"<pre>Error: {e}</pre>", 500


@app.route("/admin/db-purge-bad-pins", methods=["POST"])
@admin_required
def db_purge_bad_pins():
    """Removes any residents rows where host_pin is NULL, empty, or whitespace."""
    from data.server_db import get_connection
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("DELETE FROM residents WHERE host_pin IS NULL OR TRIM(host_pin) = ''")
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        _audit("DB_PURGE_BAD_PINS", details=f"Removed {deleted} bad-PIN rows")
        flash(f"Removed {deleted} row(s) with blank/null PINs. Try adding hosts again.", "success")
    except Exception as e:
        flash(f"Purge failed: {e}", "error")
    return redirect(url_for("manage_hosts"))


@app.route("/admin/fix-pin-constraint", methods=["POST"])
@admin_required
def fix_pin_constraint():
    """One-time fix: drop and rebuild the host_pin UNIQUE constraint cleanly."""
    from data.server_db import get_connection
    try:
        conn = get_connection()
        cur  = conn.cursor()

        # Step 1: drop ALL unique constraints on residents table
        # Must drop the CONSTRAINT (not the index) — Postgres owns the index
        cur.execute("""
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_name = 'residents'
              AND table_schema = 'public'
              AND constraint_type = 'UNIQUE'
        """)
        constraints = [r["constraint_name"] for r in cur.fetchall()]
        for c in constraints:
            cur.execute(f'ALTER TABLE residents DROP CONSTRAINT "{c}" CASCADE')

        # Step 2: also drop any orphan indexes that survived
        cur.execute("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'residents'
              AND schemaname = 'public'
              AND indexdef ILIKE '%host_pin%'
        """)
        for r in cur.fetchall():
            cur.execute(f'DROP INDEX IF EXISTS "{r["indexname"]}" CASCADE')

        # Step 3: recreate the constraint fresh
        cur.execute("""
            ALTER TABLE residents
            ADD CONSTRAINT residents_host_pin_key UNIQUE (host_pin)
        """)

        conn.commit()
        cur.close()
        conn.close()
        flash(f"PIN constraint rebuilt cleanly. You can now add hosts.", "success")
        _audit("FIX_PIN_CONSTRAINT", details=f"Dropped {constraints} and rebuilt UNIQUE(host_pin)")
    except Exception as e:
        import traceback
        flash(f"Fix failed: {e}", "error")
        print(traceback.format_exc())
    return redirect(url_for("manage_hosts"))


@app.route("/admin/full-reset-and-seed", methods=["POST"])
@admin_required
def full_reset_and_seed():
    from data.server_db import get_connection
    import uuid as _uuid
    from datetime import datetime, timezone, timedelta
    import random

    EAT = timezone(timedelta(hours=3))
    def ts(dt): return dt.strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.now(EAT).replace(tzinfo=None)

    try:
        conn = get_connection()
        cur  = conn.cursor()

        # ── WIPE EVERYTHING ───────────────────────────────────────────────
        cur.execute("DELETE FROM associated_passengers")
        cur.execute("DELETE FROM visit_logs")
        cur.execute("DELETE FROM visitors")
        cur.execute("DELETE FROM residents")

        # Fix the SERIAL sequence — this is the root cause of the null ID bug
        cur.execute("DROP SEQUENCE IF EXISTS residents_resident_id_seq CASCADE")
        cur.execute("CREATE SEQUENCE residents_resident_id_seq START 1")
        cur.execute("ALTER TABLE residents ALTER COLUMN resident_id SET DEFAULT nextval('residents_resident_id_seq')")
        cur.execute("ALTER SEQUENCE residents_resident_id_seq OWNED BY residents.resident_id")

        # ── SEED HOSTS ────────────────────────────────────────────────────
        hosts_data = [
            ("Dr. James Mwangi",    "A-101",     "1234", "0712000001", "residential"),
            ("Prof. Grace Otieno",  "A-102",     "5678", "0712000002", "residential"),
            ("Mr. Peter Kamau",     "B-201",     "2468", "0712000003", "residential"),
            ("Acme Technologies",   "Suite 301", "9999", "0700100001", "office"),
            ("Nairobi Consultants", "Suite 402", "7777", "0700100002", "office"),
            ("HR Department",       "Suite 101", "4321", "0700100003", "office"),
        ]
        units = []
        for i, (name, unit, pin, phone, htype) in enumerate(hosts_data, start=1):
            cur.execute(
                "INSERT INTO residents (resident_id, full_name, unit_number, host_pin, phone, host_type, is_active) "
                "VALUES (%s, %s, %s, %s, %s, %s, TRUE)",
                (i, name, unit, pin, phone, htype)
            )
            units.append(unit)
        # Sync sequence after manual IDs
        cur.execute("SELECT setval('residents_resident_id_seq', %s)", (len(hosts_data),))

        # ── SEED COMPLETED VISITS (20) ────────────────────────────────────
        first_names = ["Alice","Brian","Carol","David","Eve","Frank","Grace",
                       "Henry","Iris","James","Karen","Leo","Mary","Noel",
                       "Olivia","Paul","Queen","Robert","Sarah","Tom"]
        last_names  = ["Kimani","Ochieng","Waweru","Muthoni","Otieno","Kariuki"]
        categories  = ["Guest","Guest","Guest","Delivery","Maintenance"]
        reasons     = ["Job interview","Package delivery","Social visit",
                       "Meeting","Equipment check","Client visit","AC repair",""]

        random.seed(42)
        for i, fname in enumerate(first_names):
            lname   = random.choice(last_names)
            nid     = str(random.randint(10000000, 39999999))
            cat     = random.choice(categories)
            unit    = random.choice(units)
            reason  = random.choice(reasons)
            days_ago = random.randint(0, 6)
            hour_in  = random.randint(7, 16)
            dur      = random.randint(15, 180)
            ci = now.replace(hour=hour_in, minute=random.randint(0,59), second=0) - timedelta(days=days_ago)
            co = ci + timedelta(minutes=dur)
            v_uuid = str(_uuid.uuid4())
            l_uuid = str(_uuid.uuid4())
            cur.execute(
                "INSERT INTO visitors (local_uuid, full_name, national_id, category, exception_flag, created_at) "
                "VALUES (%s,%s,%s,%s,FALSE,%s)",
                (v_uuid, f"{fname} {lname}", nid, cat, ts(ci))
            )
            cur.execute(
                "INSERT INTO visit_logs (local_uuid, visitor_uuid, guard_id, pax_count, "
                "check_in_time, check_out_time, checkout_guard_id, host_unit, reason_for_visit) "
                "VALUES (%s,%s,1,1,%s,%s,1,%s,%s)",
                (l_uuid, v_uuid, ts(ci), ts(co), unit, reason or None)
            )

        # ── SEED 3 ACTIVE VISITS ──────────────────────────────────────────
        active = [
            ("John Doe",   "22345678", "Guest",       units[0], "Social visit",    45),
            ("Mary Smith", "33456789", "Delivery",    units[3], "Package delivery", 8),
            ("Ali Hassan", "44567890", "Maintenance", units[4], "AC repair",       30),
        ]
        for (name, nid, cat, unit, reason, mins_ago) in active:
            ci = now - timedelta(minutes=mins_ago)
            v_uuid = str(_uuid.uuid4())
            l_uuid = str(_uuid.uuid4())
            cur.execute(
                "INSERT INTO visitors (local_uuid, full_name, national_id, category, exception_flag, created_at) "
                "VALUES (%s,%s,%s,%s,FALSE,%s)",
                (v_uuid, name, nid, cat, ts(ci))
            )
            cur.execute(
                "INSERT INTO visit_logs (local_uuid, visitor_uuid, guard_id, pax_count, "
                "check_in_time, host_unit, reason_for_visit) "
                "VALUES (%s,%s,1,1,%s,%s,%s)",
                (l_uuid, v_uuid, ts(ci), unit, reason)
            )

        conn.commit()
        cur.close()
        conn.close()
        _audit("FULL_RESET_AND_SEED", details="Wiped all data, fixed sequence, seeded 6 hosts + 23 visits")
        flash("Reset complete — 6 hosts, 20 completed visits, 3 active visitors seeded.", "success")

    except Exception as e:
        import traceback
        flash(f"Seed failed: {e}", "error")
        print(traceback.format_exc())
        try: conn.rollback()
        except: pass

    return redirect(url_for("dashboard"))


@app.route("/admin/reset-hosts", methods=["POST"])
@admin_required
def reset_hosts():
    """Wipe all hosts so you can start fresh."""
    ok = clear_all_hosts()
    if ok:
        flash("All hosts cleared. You can now add fresh ones.", "success")
        _audit("RESET_HOSTS", details="Admin wiped all host records")
    else:
        flash("Clear failed — check logs.", "error")
    return redirect(url_for("manage_hosts"))


@app.route("/admin/reset-visits", methods=["POST"])
@admin_required
def reset_visits():
    """Wipe all visit data (visitors, visit_logs, passengers)."""
    ok = clear_all_visits()
    if ok:
        flash("All visit data cleared.", "success")
        _audit("RESET_VISITS", details="Admin wiped all visit records")
    else:
        flash("Clear failed — check logs.", "error")
    return redirect(url_for("dashboard"))



# ── EDIT GUARD (admin) ────────────────────────────────────────────────────

@app.route("/manage-guards/edit/<int:guard_id>", methods=["POST"])
@admin_required
def edit_guard(guard_id):
    username  = request.form.get("username",  "").strip()
    full_name = request.form.get("full_name", "").strip()
    role      = request.form.get("role",      "guard").strip()

    if not username or not full_name:
        flash("Username and full name are required.", "error")
        return redirect(url_for("manage_guards"))

    from data.server_db import get_connection
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE guards SET username=%s, full_name=%s, role=%s WHERE guard_id=%s",
            (username, full_name, role, guard_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        flash(f"Guard updated successfully.", "success")
        _audit("GUARD_EDIT", target=str(guard_id),
               details=f"username={username}, full_name={full_name}, role={role}")
    except Exception as e:
        flash(f"Update failed: {e}", "error")
    return redirect(url_for("manage_guards"))


# ── EDIT BLACKLIST ENTRY (admin) ──────────────────────────────────────────

@app.route("/blacklist/edit/<int:bl_id>", methods=["POST"])
@admin_required
def blacklist_edit(bl_id):
    national_id = request.form.get("national_id", "").strip()
    full_name   = request.form.get("full_name",   "").strip()
    reason      = request.form.get("reason",      "").strip()

    if not national_id or not reason:
        flash("National ID and reason are required.", "error")
        return redirect(url_for("blacklist"))

    from data.server_db import get_connection
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE blacklist SET national_id=%s, full_name=%s, reason=%s WHERE id=%s",
            (national_id, full_name, reason, bl_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        flash("Blacklist entry updated.", "success")
        _audit("BLACKLIST_EDIT", target=f"NID:{national_id}",
               details=f"reason: {reason}")
    except Exception as e:
        flash(f"Update failed: {e}", "error")
    return redirect(url_for("blacklist"))


# ── EDIT ACTIVE VISIT (admin only) ───────────────────────────────────────

@app.route("/visit/edit/<log_uuid>", methods=["POST"])
@admin_required
def edit_visit(log_uuid):
    """Admin can correct visitor details on active (not yet checked out) visits."""
    full_name   = request.form.get("full_name",   "").strip()
    national_id = request.form.get("national_id", "").strip()
    reason      = request.form.get("reason",      "").strip()
    host_unit   = request.form.get("host_unit",   "").strip()

    if not full_name:
        flash("Full name is required.", "error")
        return redirect(url_for("dashboard"))

    from data.server_db import get_connection
    try:
        conn = get_connection()
        cur  = conn.cursor()
        # Get visitor_uuid for this log
        cur.execute("SELECT visitor_uuid FROM visit_logs WHERE local_uuid=%s", (log_uuid,))
        row = cur.fetchone()
        if not row:
            flash("Visit not found.", "error")
            cur.close(); conn.close()
            return redirect(url_for("dashboard"))

        visitor_uuid = row["visitor_uuid"]

        # Update visitor details
        cur.execute(
            "UPDATE visitors SET full_name=%s, national_id=%s WHERE local_uuid=%s",
            (full_name, national_id or None, visitor_uuid)
        )
        # Update visit log details
        cur.execute(
            "UPDATE visit_logs SET reason_for_visit=%s, host_unit=%s WHERE local_uuid=%s",
            (reason or None, host_unit or None, log_uuid)
        )
        conn.commit()
        cur.close()
        conn.close()
        flash(f"Visit record corrected.", "success")
        _audit("VISIT_EDIT", target=log_uuid,
               details=f"Admin corrected: {full_name}, NID={national_id}, unit={host_unit}")
    except Exception as e:
        flash(f"Edit failed: {e}", "error")
    return redirect(url_for("dashboard"))


# ── AJAX API ENDPOINTS (return JSON, used by fetch() calls) ───────────────

@app.route("/api/guard/toggle/<int:guard_id>", methods=["POST"])
@admin_required
def api_toggle_guard(guard_id):
    result = toggle_guard_server(guard_id, requesting_guard_id=session.get("guard_id"))
    if result == "self":
        return jsonify({"ok": False, "error": "You cannot toggle your own account."}), 403
    if result is None:
        return jsonify({"ok": False, "error": "Toggle failed."}), 500
    _audit("GUARD_TOGGLE", target=str(guard_id),
           details=f"new state: {'active' if result else 'inactive'}")
    return jsonify({"ok": True, "active": result})


@app.route("/api/guard/edit/<int:guard_id>", methods=["POST"])
@admin_required
def api_edit_guard(guard_id):
    data      = request.get_json()
    username  = (data.get("username") or "").strip()
    full_name = (data.get("full_name") or "").strip()
    role      = (data.get("role") or "guard").strip()
    if not username or not full_name:
        return jsonify({"ok": False, "error": "Username and full name are required."}), 400
    from data.server_db import get_connection
    try:
        conn = get_connection(); cur = conn.cursor()
        cur.execute("UPDATE guards SET username=%s, full_name=%s, role=%s WHERE guard_id=%s",
                    (username, full_name, role, guard_id))
        conn.commit(); cur.close(); conn.close()
        _audit("GUARD_EDIT", target=str(guard_id),
               details=f"username={username}, full_name={full_name}, role={role}")
        return jsonify({"ok": True, "username": username,
                        "full_name": full_name, "role": role})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/guard/reset-password/<int:guard_id>", methods=["POST"])
@admin_required
def api_reset_password(guard_id):
    data = request.get_json()
    pw   = (data.get("password") or "").strip()
    if len(pw) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters."}), 400
    ok = reset_guard_password_server(guard_id, pw)
    if ok:
        _audit("GUARD_PASSWORD_RESET", target=str(guard_id))
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Reset failed."}), 500


@app.route("/api/host/toggle/<int:host_id>", methods=["POST"])
@admin_required
def api_toggle_host(host_id):
    new_state = toggle_resident_server(host_id)
    _audit("HOST_TOGGLE", target=str(host_id),
           details=f"new state: {'active' if new_state else 'inactive'}")
    return jsonify({"ok": True, "active": new_state})


@app.route("/api/host/edit/<int:host_id>", methods=["POST"])
@admin_required
def api_edit_host(host_id):
    data        = request.get_json()
    full_name   = (data.get("full_name")   or "").strip()
    unit_number = (data.get("unit_number") or "").strip()
    host_pin    = (data.get("host_pin")    or "").strip()
    phone       = (data.get("phone")       or "").strip()
    host_type   = (data.get("host_type")   or "residential").strip()
    if not full_name or not unit_number or not host_pin:
        return jsonify({"ok": False, "error": "Name, unit and PIN are required."}), 400
    if not host_pin.isdigit() or len(host_pin) < 4:
        return jsonify({"ok": False, "error": "PIN must be numbers only, minimum 4 digits."}), 400
    from data.server_db import get_connection
    try:
        conn = get_connection(); cur = conn.cursor()
        cur.execute("""UPDATE residents
                       SET full_name=%s, unit_number=%s, host_pin=%s,
                           phone=%s, host_type=%s
                       WHERE resident_id=%s""",
                    (full_name, unit_number, host_pin, phone or None,
                     host_type, host_id))
        conn.commit(); cur.close(); conn.close()
        _audit("HOST_EDIT", target=str(host_id),
               details=f"{full_name} @ {unit_number}, PIN updated")
        return jsonify({"ok": True, "full_name": full_name,
                        "unit_number": unit_number, "host_pin": host_pin,
                        "phone": phone, "host_type": host_type})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/blacklist/edit/<int:bl_id>", methods=["POST"])
@admin_required
def api_edit_blacklist(bl_id):
    data        = request.get_json()
    national_id = (data.get("national_id") or "").strip()
    full_name   = (data.get("full_name")   or "").strip()
    reason      = (data.get("reason")      or "").strip()
    if not national_id or not reason:
        return jsonify({"ok": False, "error": "National ID and reason are required."}), 400
    if not national_id.isdigit():
        return jsonify({"ok": False, "error": "National ID must be numbers only."}), 400
    from data.server_db import get_connection
    try:
        conn = get_connection(); cur = conn.cursor()
        cur.execute("UPDATE blacklist SET national_id=%s, full_name=%s, reason=%s WHERE id=%s",
                    (national_id, full_name, reason, bl_id))
        conn.commit(); cur.close(); conn.close()
        _audit("BLACKLIST_EDIT", target=f"NID:{national_id}", details=reason)
        return jsonify({"ok": True, "national_id": national_id,
                        "full_name": full_name, "reason": reason})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/blacklist/remove/<int:bl_id>", methods=["POST"])
@admin_required
def api_remove_blacklist(bl_id):
    ok = remove_blacklist(bl_id)
    if ok:
        _audit("BLACKLIST_REMOVE", target=str(bl_id))
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Remove failed."}), 500


@app.route("/api/host/add", methods=["POST"])
@admin_required
def api_add_host():
    data        = request.get_json()
    full_name   = (data.get("full_name")   or "").strip()
    unit_number = (data.get("unit_number") or "").strip()
    host_pin    = (data.get("host_pin")    or "").strip()
    phone       = (data.get("phone")       or "").strip()
    host_type   = (data.get("host_type")   or "residential").strip()
    if not full_name or not unit_number or not host_pin:
        return jsonify({"ok": False, "error": "Name, unit and PIN are required."}), 400
    if not host_pin.isdigit() or len(host_pin) < 4:
        return jsonify({"ok": False, "error": "PIN must be numbers only, minimum 4 digits."}), 400
    ok = add_resident_server(full_name, unit_number, host_pin, phone, host_type, None)
    if ok:
        _audit("HOST_ADD", target=unit_number, details=f"{full_name} ({host_type})")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "PIN already exists. Choose a different PIN."}), 400


@app.route("/api/guard/add", methods=["POST"])
@admin_required
def api_add_guard():
    data      = request.get_json()
    username  = (data.get("username")  or "").strip()
    full_name = (data.get("full_name") or "").strip()
    password  = (data.get("password")  or "").strip()
    role      = (data.get("role")      or "guard").strip()
    if not username or not full_name or not password:
        return jsonify({"ok": False, "error": "All fields are required."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters."}), 400
    ok = add_guard_server(username, password, full_name, role)
    if ok:
        _audit("GUARD_ADD", target=username, details=f"role={role}")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": f"Username '{username}' already exists."}), 400


@app.route("/api/blacklist/add", methods=["POST"])
@admin_required
def api_add_blacklist():
    data        = request.get_json()
    national_id = (data.get("national_id") or "").strip()
    full_name   = (data.get("full_name")   or "").strip()
    reason      = (data.get("reason")      or "").strip()
    if not national_id or not reason:
        return jsonify({"ok": False, "error": "National ID and reason are required."}), 400
    if not national_id.isdigit():
        return jsonify({"ok": False, "error": "National ID must be numbers only."}), 400
    ok = add_blacklist(national_id, full_name, reason, session.get("guard_id"))
    if ok:
        _audit("BLACKLIST_ADD", target=f"NID:{national_id}", details=reason)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Failed to add to blacklist."}), 400


@app.route("/api/visit/edit/<log_uuid>", methods=["POST"])
@admin_required
def api_edit_visit(log_uuid):
    data        = request.get_json()
    full_name   = (data.get("full_name")   or "").strip()
    national_id = (data.get("national_id") or "").strip()
    reason      = (data.get("reason")      or "").strip()
    host_unit   = (data.get("host_unit")   or "").strip()
    if not full_name:
        return jsonify({"ok": False, "error": "Full name is required."}), 400
    from data.server_db import get_connection
    try:
        conn = get_connection(); cur = conn.cursor()
        cur.execute("SELECT visitor_uuid FROM visit_logs WHERE local_uuid=%s", (log_uuid,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Visit not found."}), 404
        cur.execute("UPDATE visitors SET full_name=%s, national_id=%s WHERE local_uuid=%s",
                    (full_name, national_id or None, row["visitor_uuid"]))
        cur.execute("UPDATE visit_logs SET reason_for_visit=%s, host_unit=%s WHERE local_uuid=%s",
                    (reason or None, host_unit or None, log_uuid))
        conn.commit(); cur.close(); conn.close()
        _audit("VISIT_EDIT", target=log_uuid,
               details=f"Admin corrected: {full_name}, NID={national_id}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── STARTUP ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)