"""
=============================================================================
server/data/server_db.py  —  PostgreSQL data layer (v3)

NEW IN v3:
  • residents table → adds host_type ('office' | 'residential') + host_email
  • visit_logs     → adds reason_for_visit
  • audit_log      → NEW table, every important action recorded
  • blacklist      → NEW table, flag visitors by national_id
  • Repeat-visitor lookup (by national_id) for autofill
  • Audit helpers (record_audit, get_audit_log)
  • Blacklist helpers (add/remove/list/check)
  • Host email lookup (used for SMTP notification)

ALL schema migrations use ADD COLUMN IF NOT EXISTS — safe on every boot.
=============================================================================
"""

import os
import hashlib
import psycopg2
import psycopg2.extras
from psycopg2 import pool as psycopg2_pool

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ── CONNECTION POOL ──────────────────────────────────────────────────────────
# Keeps 2-10 persistent connections alive between requests.
# Eliminates 100-300ms TCP handshake overhead on every request.
_pool = None

def _get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL environment variable is not set.")
        _pool = psycopg2_pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool


def get_connection():
    """Borrows a connection from the pool. Call .close() to return it."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return _get_pool().getconn()


def return_connection(conn, error=False):
    """Return a connection to the pool. Call after every get_connection()."""
    try:
        if error:
            conn.rollback()
        _get_pool().putconn(conn)
    except Exception:
        pass


# ── SCHEMA ─────────────────────────────────────────────────────────────────

def init_server_db():
    conn = get_connection()
    cur  = conn.cursor()

    # Guards
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guards (
            guard_id   SERIAL PRIMARY KEY,
            username   TEXT    NOT NULL UNIQUE,
            full_name  TEXT    NOT NULL,
            password   TEXT    NOT NULL DEFAULT '',
            role       TEXT    NOT NULL DEFAULT 'guard',
            is_active  BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    for col, definition in [
        ("password", "TEXT NOT NULL DEFAULT ''"),
        ("role",     "TEXT NOT NULL DEFAULT 'guard'"),
    ]:
        try:
            cur.execute(
                f"ALTER TABLE guards ADD COLUMN IF NOT EXISTS {col} {definition}"
            )
        except Exception:
            conn.rollback()

    default_pw = hashlib.sha256(b"admin123").hexdigest()
    cur.execute("""
        INSERT INTO guards (username, full_name, password, role, is_active)
        VALUES ('admin', 'System Administrator', %s, 'admin', TRUE)
        ON CONFLICT (username) DO NOTHING
    """, (default_pw,))

    # Hosts (table still named "residents" for backwards compat)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS residents (
            resident_id SERIAL PRIMARY KEY,
            full_name   TEXT NOT NULL,
            unit_number TEXT NOT NULL,
            host_pin    TEXT NOT NULL UNIQUE,
            phone       TEXT,
            host_type   TEXT NOT NULL DEFAULT 'residential',
            host_email  TEXT,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)
    for col, definition in [
        ("host_type",  "TEXT NOT NULL DEFAULT 'residential'"),
        ("host_email", "TEXT"),
    ]:
        try:
            cur.execute(
                f"ALTER TABLE residents ADD COLUMN IF NOT EXISTS {col} {definition}"
            )
        except Exception:
            conn.rollback()

    # Visitors
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visitors (
            local_uuid      TEXT    PRIMARY KEY,
            full_name       TEXT    NOT NULL,
            national_id     TEXT,
            phone_number    TEXT,
            vehicle_plate   TEXT,
            category        TEXT    NOT NULL,
            exception_flag  BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TEXT    NOT NULL
        )
    """)

    # Visit logs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visit_logs (
            local_uuid        TEXT    PRIMARY KEY,
            visitor_uuid      TEXT    NOT NULL REFERENCES visitors(local_uuid),
            guard_id          INTEGER,
            resident_id       INTEGER,
            pax_count         INTEGER NOT NULL DEFAULT 1,
            estimated_minutes INTEGER,
            check_in_time     TEXT    NOT NULL,
            check_out_time    TEXT,
            checkout_guard_id INTEGER,
            host_unit         TEXT,
            reason_for_visit  TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    for col, definition in [
        ("checkout_guard_id", "INTEGER"),
        ("host_unit",         "TEXT"),
        ("reason_for_visit",  "TEXT"),
    ]:
        try:
            cur.execute(
                f"ALTER TABLE visit_logs ADD COLUMN IF NOT EXISTS {col} {definition}"
            )
        except Exception:
            conn.rollback()

    # Associated passengers
    cur.execute("""
        CREATE TABLE IF NOT EXISTS associated_passengers (
            id          SERIAL PRIMARY KEY,
            log_uuid    TEXT NOT NULL REFERENCES visit_logs(local_uuid),
            national_id TEXT NOT NULL,
            full_name   TEXT,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (log_uuid, national_id)
        )
    """)
    # Add full_name column if upgrading from old schema
    try:
        cur.execute("ALTER TABLE associated_passengers ADD COLUMN IF NOT EXISTS full_name TEXT")
    except Exception:
        conn.rollback()

    # Audit log — append-only record of every important action
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id           SERIAL PRIMARY KEY,
            occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            actor_guard  INTEGER,
            actor_name   TEXT,
            action       TEXT NOT NULL,
            target       TEXT,
            details      TEXT,
            ip_address   TEXT
        )
    """)

    # Pre-registered visitors (host submits expected visitors in advance)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pre_registrations (
            id              SERIAL PRIMARY KEY,
            resident_id     INTEGER NOT NULL,
            host_unit       TEXT    NOT NULL,
            visitor_name    TEXT    NOT NULL,
            national_id     TEXT,
            visit_date      DATE    NOT NULL,
            time_from       TIME,
            time_to         TIME,
            reason          TEXT,
            is_used         BOOLEAN NOT NULL DEFAULT FALSE,
            used_at         TIMESTAMPTZ,
            added_by        INTEGER,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # Blacklist — flagged national IDs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            id          SERIAL PRIMARY KEY,
            national_id TEXT NOT NULL UNIQUE,
            full_name   TEXT,
            reason      TEXT NOT NULL,
            added_by    INTEGER,
            added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            is_active   BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

    conn.commit()
    cur.close()
    return_connection(conn)
    print("[ServerDB] Tables verified/created successfully.")


# ── AUDIT LOG ─────────────────────────────────────────────────────────────

def record_audit(actor_guard_id, actor_name: str, action: str,
                 target: str = None, details: str = None,
                 ip_address: str = None) -> bool:
    """
    Records an action to the audit log. Never raises — audit failures must
    not block the action they're describing.
    """
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO audit_log
                (actor_guard, actor_name, action, target, details, ip_address)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (actor_guard_id, actor_name, action, target, details, ip_address))
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[Audit] record_audit error: {e}")
        return False


def get_audit_log(limit: int = 200, action_filter: str = None,
                  actor_filter: str = None) -> list:
    try:
        conn  = get_connection()
        cur   = conn.cursor()
        query = """
            SELECT id, occurred_at, actor_guard, actor_name,
                   action, target, details, ip_address
            FROM audit_log
            WHERE 1=1
        """
        params = []
        if action_filter:
            query += " AND action ILIKE %s"
            params.append(f"%{action_filter}%")
        if actor_filter:
            query += " AND actor_name ILIKE %s"
            params.append(f"%{actor_filter}%")
        query += " ORDER BY occurred_at DESC LIMIT %s"
        params.append(limit)
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        return_connection(conn)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[Audit] get_audit_log error: {e}")
        return []


# ── BLACKLIST ─────────────────────────────────────────────────────────────

def check_blacklist(national_id: str):
    """Returns blacklist row dict if national_id is flagged, else None."""
    if not national_id:
        return None
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT id, national_id, full_name, reason, added_at
            FROM blacklist
            WHERE national_id = %s AND is_active = TRUE
        """, (national_id,))
        row = cur.fetchone()
        cur.close()
        return_connection(conn)
        return dict(row) if row else None
    except Exception as e:
        print(f"[Blacklist] check error: {e}")
        return None


def add_blacklist(national_id: str, full_name: str,
                  reason: str, added_by: int) -> bool:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO blacklist (national_id, full_name, reason, added_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (national_id) DO UPDATE SET
                full_name  = EXCLUDED.full_name,
                reason     = EXCLUDED.reason,
                added_by   = EXCLUDED.added_by,
                is_active  = TRUE,
                added_at   = NOW()
        """, (national_id, full_name, reason, added_by))
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[Blacklist] add error: {e}")
        return False


def remove_blacklist(blacklist_id: int) -> bool:
    """Soft-delete (sets is_active = FALSE) so audit history is preserved."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE blacklist SET is_active = FALSE WHERE id = %s",
            (blacklist_id,),
        )
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[Blacklist] remove error: {e}")
        return False


def get_all_blacklist() -> list:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT b.id, b.national_id, b.full_name, b.reason,
                   b.added_at, b.is_active, g.full_name AS added_by_name
            FROM blacklist b
            LEFT JOIN guards g ON g.guard_id = b.added_by
            WHERE b.is_active = TRUE
            ORDER BY b.added_at DESC
        """)
        rows = cur.fetchall()
        cur.close()
        return_connection(conn)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[Blacklist] get_all error: {e}")
        return []


# ── REPEAT VISITOR AUTOFILL ───────────────────────────────────────────────

def find_visitor_by_national_id(national_id: str):
    """
    Returns the most recent visitor record with this national_id (for
    autofill). Returns None if no match.
    """
    if not national_id or not national_id.isdigit():
        return None
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT v.full_name, v.phone_number, v.vehicle_plate
            FROM visitors v
            JOIN visit_logs vl ON vl.visitor_uuid = v.local_uuid
            WHERE v.national_id = %s
            ORDER BY vl.check_in_time DESC
            LIMIT 1
        """, (national_id,))
        row = cur.fetchone()
        cur.close()
        return_connection(conn)
        return dict(row) if row else None
    except Exception as e:
        print(f"[ServerDB] find_visitor error: {e}")
        return None


# ── HOST EMAIL LOOKUP (for notification) ──────────────────────────────────

def get_host_by_unit(unit_number: str):
    """Returns the active host record for a unit (used for email notifications)."""
    if not unit_number:
        return None
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT resident_id, full_name, unit_number, host_email,
                   host_type, phone
            FROM residents
            WHERE unit_number = %s AND is_active = TRUE
            LIMIT 1
        """, (unit_number,))
        row = cur.fetchone()
        cur.close()
        return_connection(conn)
        return dict(row) if row else None
    except Exception as e:
        print(f"[ServerDB] get_host_by_unit error: {e}")
        return None


# ── WRITE OPERATIONS ──────────────────────────────────────────────────────

def upsert_visit(data: dict) -> bool:
    try:
        conn = get_connection()
        cur  = conn.cursor()

        cur.execute("""
            INSERT INTO visitors
                (local_uuid, full_name, national_id, phone_number,
                 vehicle_plate, category, exception_flag, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (local_uuid) DO UPDATE SET
                full_name      = EXCLUDED.full_name,
                national_id    = EXCLUDED.national_id,
                phone_number   = EXCLUDED.phone_number,
                vehicle_plate  = EXCLUDED.vehicle_plate,
                category       = EXCLUDED.category,
                exception_flag = EXCLUDED.exception_flag
        """, (
            data["visitor_uuid"], data["full_name"], data.get("national_id"),
            data.get("phone_number"), data.get("vehicle_plate"),
            data["category"], bool(data.get("exception_flag", False)),
        ))

        cur.execute("""
            INSERT INTO visit_logs
                (local_uuid, visitor_uuid, guard_id, resident_id,
                 pax_count, estimated_minutes, check_in_time, check_out_time,
                 host_unit, reason_for_visit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (local_uuid) DO UPDATE SET
                check_out_time    = EXCLUDED.check_out_time,
                pax_count         = EXCLUDED.pax_count,
                estimated_minutes = EXCLUDED.estimated_minutes,
                host_unit         = EXCLUDED.host_unit,
                reason_for_visit  = EXCLUDED.reason_for_visit
        """, (
            data["log_uuid"], data["visitor_uuid"],
            data.get("guard_id"), data.get("resident_id"),
            data.get("pax_count", 1), data.get("estimated_minutes"),
            data["check_in_time"], data.get("check_out_time"),
            data.get("host_unit"), data.get("reason_for_visit"),
        ))

        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[ServerDB] upsert_visit error: {e}")
        return False


def upsert_passenger(log_uuid: str, national_id: str, full_name: str = None) -> bool:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO associated_passengers (log_uuid, national_id, full_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (log_uuid, national_id) DO NOTHING
        """, (log_uuid, national_id, full_name or None))
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[ServerDB] upsert_passenger error: {e}")
        return False


def web_checkout(log_uuid: str, guard_id: int = None) -> bool:
    from datetime import datetime, timezone, timedelta
    eat = timezone(timedelta(hours=3))
    now_eat = datetime.now(eat).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE visit_logs
            SET check_out_time    = %s,
                checkout_guard_id = %s
            WHERE local_uuid = %s AND check_out_time IS NULL
        """, (now_eat, guard_id, log_uuid))
        conn.commit()
        rows = cur.rowcount
        cur.close()
        return_connection(conn)
        return rows > 0
    except Exception as e:
        print(f"[ServerDB] web_checkout error: {e}")
        return False


def get_visit_for_audit(log_uuid: str):
    """Lightweight visitor name lookup for audit log details."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT v.full_name FROM visit_logs vl
            JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            WHERE vl.local_uuid = %s
        """, (log_uuid,))
        row = cur.fetchone()
        cur.close()
        return_connection(conn)
        return dict(row) if row else None
    except Exception:
        return None


# ── READ OPERATIONS ───────────────────────────────────────────────────────

def get_active_visits_server(host_unit_filter: str = None) -> list:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        query = """
            SELECT
                vl.local_uuid, v.full_name, v.category, v.vehicle_plate,
                v.national_id, v.phone_number,
                vl.pax_count, vl.check_in_time, vl.estimated_minutes,
                vl.host_unit, vl.reason_for_visit,
                v.exception_flag,
                g_in.full_name AS checkin_guard_name,
                COALESCE(STRING_AGG(
                CASE WHEN ap.full_name IS NOT NULL
                     THEN ap.full_name || ' (' || ap.national_id || ')'
                     ELSE ap.national_id
                END, ' | '), '—') AS pax_ids
            FROM visit_logs vl
            JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            LEFT JOIN associated_passengers ap ON ap.log_uuid = vl.local_uuid
            LEFT JOIN guards g_in ON g_in.guard_id = vl.guard_id
            WHERE vl.check_out_time IS NULL
        """
        params = []
        if host_unit_filter:
            query += " AND vl.host_unit ILIKE %s"
            params.append(f"%{host_unit_filter}%")
        query += """
            GROUP BY vl.local_uuid, v.full_name, v.category, v.vehicle_plate,
                     v.national_id, v.phone_number,
                     vl.pax_count, vl.check_in_time, vl.estimated_minutes,
                     vl.host_unit, vl.reason_for_visit,
                     v.exception_flag, g_in.full_name
            ORDER BY vl.check_in_time DESC
        """
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        return_connection(conn)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_active_visits error: {e}")
        return []


def get_filtered_history(category: str = None,
                         date_from: str = None, date_to: str = None,
                         time_from: str = None, time_to: str = None,
                         host_unit: str = None,
                         limit: int = 500) -> list:
    try:
        conn  = get_connection()
        cur   = conn.cursor()
        query = """
            SELECT
                vl.local_uuid, v.full_name, v.national_id, v.category,
                vl.pax_count, vl.check_in_time, vl.check_out_time,
                vl.host_unit, vl.reason_for_visit,
                v.exception_flag,
                g_in.full_name  AS checkin_guard_name,
                g_out.full_name AS checkout_guard_name,
                COALESCE(STRING_AGG(
                CASE WHEN ap.full_name IS NOT NULL
                     THEN ap.full_name || ' (' || ap.national_id || ')'
                     ELSE ap.national_id
                END, ' | '), '—') AS pax_ids,
                CASE
                    WHEN v.category = 'Delivery'
                     AND EXTRACT(EPOCH FROM (
                         vl.check_out_time::timestamp -
                         vl.check_in_time::timestamp
                     )) / 60.0 > 20
                    THEN TRUE ELSE FALSE
                END AS was_overdue
            FROM visit_logs vl
            JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            LEFT JOIN associated_passengers ap ON ap.log_uuid = vl.local_uuid
            LEFT JOIN guards g_in  ON g_in.guard_id  = vl.guard_id
            LEFT JOIN guards g_out ON g_out.guard_id = vl.checkout_guard_id
            WHERE vl.check_out_time IS NOT NULL
        """
        params = []
        if category:
            query += " AND v.category = %s"; params.append(category)
        if date_from:
            query += " AND vl.check_in_time::date >= %s"; params.append(date_from)
        if date_to:
            query += " AND vl.check_in_time::date <= %s"; params.append(date_to)
        if time_from:
            query += " AND vl.check_in_time::time >= %s"; params.append(time_from)
        if time_to:
            query += " AND vl.check_in_time::time <= %s"; params.append(time_to)
        if host_unit:
            query += " AND vl.host_unit ILIKE %s"; params.append(f"%{host_unit}%")
        query += """
            GROUP BY vl.local_uuid, v.full_name, v.national_id, v.category,
                     vl.pax_count, vl.check_in_time, vl.check_out_time,
                     vl.host_unit, vl.reason_for_visit,
                     v.exception_flag, g_in.full_name, g_out.full_name
            ORDER BY vl.check_in_time DESC
            LIMIT %s
        """
        params.append(limit)
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        return_connection(conn)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_filtered_history error: {e}")
        return []


def get_stats_server() -> dict:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM visit_logs")
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) AS today FROM visit_logs "
                    "WHERE check_in_time::date = CURRENT_DATE")
        today = cur.fetchone()["today"]
        cur.execute("SELECT COUNT(*) AS active FROM visit_logs "
                    "WHERE check_out_time IS NULL")
        active = cur.fetchone()["active"]
        cur.execute("""
            SELECT v.category, COUNT(*) AS cnt
            FROM visit_logs vl JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            GROUP BY v.category
        """)
        by_cat = {row["category"]: row["cnt"] for row in cur.fetchall()}
        cur.close()
        return_connection(conn)
        return {"total": total, "today": today, "active": active,
                "by_category": by_cat}
    except Exception as e:
        print(f"[ServerDB] get_stats error: {e}")
        return {"total": 0, "today": 0, "active": 0, "by_category": {}}


def get_active_units() -> list:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT DISTINCT u FROM (
                SELECT host_unit AS u FROM visit_logs WHERE host_unit IS NOT NULL
                UNION
                SELECT unit_number AS u FROM residents WHERE is_active = TRUE
            ) sub WHERE u IS NOT NULL AND u <> ''
            ORDER BY u
        """)
        rows = cur.fetchall()
        cur.close()
        return_connection(conn)
        return [r["u"] for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_active_units error: {e}")
        return []




def clear_all_hosts() -> bool:
    """Wipe all rows from residents table. Admin-only, used for reset."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("DELETE FROM residents")
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[ServerDB] clear_all_hosts error: {e}")
        return False


def clear_all_visits() -> bool:
    """Wipe visit_logs, visitors, associated_passengers. Admin-only reset."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("DELETE FROM associated_passengers")
        cur.execute("DELETE FROM visit_logs")
        cur.execute("DELETE FROM visitors")
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[ServerDB] clear_all_visits error: {e}")
        return False



# ── PRE-REGISTRATION ──────────────────────────────────────────────────────

def add_pre_registration(resident_id, host_unit, visitor_name,
                         national_id, visit_date, time_from,
                         time_to, reason, added_by) -> bool:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO pre_registrations
                (resident_id, host_unit, visitor_name, national_id,
                 visit_date, time_from, time_to, reason, added_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (resident_id, host_unit, visitor_name, national_id or None,
              visit_date, time_from or None, time_to or None,
              reason or None, added_by))
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[ServerDB] add_pre_registration error: {e}")
        return False


def get_pre_registrations(host_unit=None, visit_date=None,
                          include_used=False) -> list:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        query = """
            SELECT pr.*, r.full_name AS host_name, r.phone AS host_phone,
                   g.full_name AS added_by_name
            FROM pre_registrations pr
            JOIN residents r ON r.resident_id = pr.resident_id
            LEFT JOIN guards g ON g.guard_id = pr.added_by
            WHERE 1=1
        """
        params = []
        if not include_used:
            query += " AND pr.is_used = FALSE"
        if host_unit:
            query += " AND pr.host_unit = %s"; params.append(host_unit)
        if visit_date:
            query += " AND pr.visit_date = %s"; params.append(visit_date)
        query += " ORDER BY pr.visit_date, pr.time_from"
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        return_connection(conn)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_pre_registrations error: {e}")
        return []


def check_pre_registration(national_id: str, visit_date: str) -> dict:
    """
    Checks if a visitor with this national_id has a valid pre-registration
    for today. Returns the pre-registration record or None.
    """
    if not national_id:
        return None
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT pr.*, r.full_name AS host_name, r.phone AS host_phone
            FROM pre_registrations pr
            JOIN residents r ON r.resident_id = pr.resident_id
            WHERE pr.national_id = %s
              AND pr.visit_date  = %s
              AND pr.is_used     = FALSE
            ORDER BY pr.created_at DESC
            LIMIT 1
        """, (national_id, visit_date))
        row = cur.fetchone()
        cur.close()
        return_connection(conn)
        return dict(row) if row else None
    except Exception as e:
        print(f"[ServerDB] check_pre_registration error: {e}")
        return None


def mark_pre_registration_used(pr_id: int) -> bool:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE pre_registrations
            SET is_used=TRUE, used_at=NOW()
            WHERE id=%s
        """, (pr_id,))
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[ServerDB] mark_pre_registration_used error: {e}")
        return False


def delete_pre_registration(pr_id: int) -> bool:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("DELETE FROM pre_registrations WHERE id=%s", (pr_id,))
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[ServerDB] delete_pre_registration error: {e}")
        return False



def get_recent_checkouts(limit: int = 10) -> list:
    """Last N unique checked-out visitors sorted by most recent checkout."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (v.national_id)
                vl.local_uuid, vl.visitor_uuid,
                v.full_name, v.national_id, v.phone_number,
                v.vehicle_plate, v.category,
                vl.host_unit, vl.reason_for_visit,
                vl.check_out_time
            FROM visit_logs vl
            JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            WHERE vl.check_out_time IS NOT NULL
              AND v.national_id IS NOT NULL
            ORDER BY v.national_id, vl.check_out_time DESC
        """)
        all_rows = cur.fetchall()
        cur.close()
        return_connection(conn)
        # Sort by check_out_time descending AFTER deduplication, take top N
        sorted_rows = sorted(
            [dict(r) for r in all_rows],
            key=lambda r: r['check_out_time'] or '',
            reverse=True
        )
        return sorted_rows[:limit]
    except Exception as e:
        print(f"[ServerDB] get_recent_checkouts error: {e}")
        return []


def find_visitor_by_national_id(national_id: str):
    """Returns last visit details for autofill including unit/category/reason."""
    if not national_id or not national_id.isdigit():
        return None
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT v.full_name, v.phone_number, v.vehicle_plate,
                   vl.host_unit, vl.reason_for_visit, v.category
            FROM visitors v
            JOIN visit_logs vl ON vl.visitor_uuid = v.local_uuid
            WHERE v.national_id = %s
            ORDER BY vl.check_in_time DESC LIMIT 1
        """, (national_id,))
        row = cur.fetchone()
        cur.close()
        return_connection(conn)
        return dict(row) if row else None
    except Exception as e:
        print(f"[ServerDB] find_visitor error: {e}")
        return None


# ── AUTH ──────────────────────────────────────────────────────────────────

def verify_guard_web(username: str, password: str):
    hashed = hashlib.sha256(password.encode()).hexdigest()
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT guard_id, username, full_name, role
            FROM guards
            WHERE username = %s AND password = %s AND is_active = TRUE
        """, (username, hashed))
        guard = cur.fetchone()
        cur.close()
        return_connection(conn)
        return dict(guard) if guard else None
    except Exception as e:
        print(f"[ServerDB] verify_guard_web error: {e}")
        return None


# ── GUARD MANAGEMENT ──────────────────────────────────────────────────────

def get_all_guards_server() -> list:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT guard_id, username, full_name, role, is_active, created_at
            FROM guards ORDER BY guard_id
        """)
        rows = cur.fetchall()
        cur.close()
        return_connection(conn)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_all_guards error: {e}")
        return []


def add_guard_server(username: str, password: str,
                     full_name: str, role: str = "guard") -> bool:
    hashed = hashlib.sha256(password.encode()).hexdigest()
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO guards (username, full_name, password, role)
            VALUES (%s, %s, %s, %s)
        """, (username, full_name, hashed, role))
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except psycopg2.IntegrityError:
        return False
    except Exception as e:
        print(f"[ServerDB] add_guard error: {e}")
        return False


def toggle_guard_server(guard_id: int, requesting_guard_id: int = None):
    if requesting_guard_id is not None and int(requesting_guard_id) == int(guard_id):
        return "self"
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE guards SET is_active = NOT is_active
            WHERE guard_id = %s RETURNING is_active
        """, (guard_id,))
        result = cur.fetchone()
        conn.commit()
        cur.close()
        return_connection(conn)
        return bool(result["is_active"]) if result else None
    except Exception as e:
        print(f"[ServerDB] toggle_guard error: {e}")
        return None


def reset_guard_password_server(guard_id: int, new_password: str) -> bool:
    hashed = hashlib.sha256(new_password.encode()).hexdigest()
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("UPDATE guards SET password = %s WHERE guard_id = %s",
                    (hashed, guard_id))
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[ServerDB] reset_guard_password error: {e}")
        return False


# ── HOST MANAGEMENT (table = residents, UI = Hosts) ───────────────────────

def get_all_residents_server() -> list:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT resident_id, full_name, unit_number, host_pin, phone,
                   host_type, host_email, is_active
            FROM residents ORDER BY unit_number, resident_id
        """)
        rows = cur.fetchall()
        cur.close()
        return_connection(conn)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_all_residents error: {e}")
        return []


def get_active_hosts_for_dropdown() -> list:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT resident_id, full_name, unit_number, host_type
            FROM residents
            WHERE is_active = TRUE
            ORDER BY unit_number, full_name
        """)
        rows = cur.fetchall()
        cur.close()
        return_connection(conn)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_active_hosts_for_dropdown error: {e}")
        return []


def add_resident_server(full_name: str, unit_number: str,
                        host_pin: str, phone: str,
                        host_type: str = "residential",
                        host_email: str = None) -> bool:
    if host_type not in ("office", "residential"):
        host_type = "residential"
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO residents
                (full_name, unit_number, host_pin, phone, host_type, host_email)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (full_name, unit_number, host_pin, phone or None,
              host_type, host_email or None))
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except psycopg2.IntegrityError as e:
        print(f"[ServerDB] add_resident IntegrityError: {e}")
        try:
            conn.rollback()
            return_connection(conn)
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"[ServerDB] add_resident error: {e}")
        try:
            conn.rollback()
            return_connection(conn)
        except Exception:
            pass
        return False


def update_resident_server(resident_id: int, full_name: str,
                           unit_number: str, phone: str,
                           host_type: str = None,
                           host_email: str = None) -> bool:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        if host_type and host_type in ("office", "residential"):
            cur.execute("""
                UPDATE residents
                SET full_name=%s, unit_number=%s, phone=%s,
                    host_type=%s, host_email=%s
                WHERE resident_id=%s
            """, (full_name, unit_number, phone or None,
                  host_type, host_email or None, resident_id))
        else:
            cur.execute("""
                UPDATE residents
                SET full_name=%s, unit_number=%s, phone=%s,
                    host_email=%s
                WHERE resident_id=%s
            """, (full_name, unit_number, phone or None,
                  host_email or None, resident_id))
        conn.commit()
        cur.close()
        return_connection(conn)
        return True
    except Exception as e:
        print(f"[ServerDB] update_resident error: {e}")
        return False


def toggle_resident_server(resident_id: int) -> bool:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE residents SET is_active = NOT is_active
            WHERE resident_id = %s RETURNING is_active
        """, (resident_id,))
        result = cur.fetchone()
        conn.commit()
        cur.close()
        return_connection(conn)
        return bool(result["is_active"]) if result else False
    except Exception as e:
        print(f"[ServerDB] toggle_resident error: {e}")
        return False