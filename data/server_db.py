"""
=============================================================================
server/data/server_db.py  —  PostgreSQL data layer (Railway)

This is the standalone web-app version. The desktop sync layer has been
removed, but the underlying tables and write helpers are unchanged because the
web check-in/check-out flow uses the same schema.

Functions used by app.py:
  init_server_db                 — create tables on first boot
  upsert_visit                   — insert visitor + visit log (web check-in)
  upsert_passenger               — insert associated passenger row
  web_checkout                   — stamp check_out_time
  get_active_visits_server       — dashboard table
  get_visit_history_server       — full history (unfiltered)
  get_filtered_history           — reports page (with filters + overdue flag)
  get_stats_server               — top-of-page metric cards
  verify_guard_web               — login auth
  get_all_guards_server          — admin: list guards
  add_guard_server               — admin: add guard
  toggle_guard_server            — admin: activate/deactivate
  reset_guard_password_server    — admin: change password
  get_all_residents_server       — admin: list residents
  add_resident_server            — admin: add resident
  update_resident_server         — admin: edit resident
  toggle_resident_server         — admin: activate/deactivate
=============================================================================
"""

import os
import hashlib
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set.\n"
            "On Railway: add a PostgreSQL database to your project and Railway "
            "will inject DATABASE_URL automatically. For local dev, export it "
            "yourself before running app.py."
        )
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# ── SCHEMA ─────────────────────────────────────────────────────────────────

def init_server_db():
    """
    Creates all tables if they don't exist. Safe to run on every boot.
    """
    conn = get_connection()
    cur  = conn.cursor()

    # Guards (web users)
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

    # Default admin user — password "admin123" — change immediately in prod
    default_pw = hashlib.sha256(b"admin123").hexdigest()
    cur.execute("""
        INSERT INTO guards (username, full_name, password, role, is_active)
        VALUES ('admin', 'System Administrator', %s, 'admin', TRUE)
        ON CONFLICT (username) DO NOTHING
    """, (default_pw,))

    # Residents
    cur.execute("""
        CREATE TABLE IF NOT EXISTS residents (
            resident_id SERIAL PRIMARY KEY,
            full_name   TEXT NOT NULL,
            unit_number TEXT NOT NULL,
            host_pin    TEXT NOT NULL UNIQUE,
            phone       TEXT,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

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
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # Associated passengers (multi-pax check-ins)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS associated_passengers (
            id          SERIAL PRIMARY KEY,
            log_uuid    TEXT NOT NULL REFERENCES visit_logs(local_uuid),
            national_id TEXT NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (log_uuid, national_id)
        )
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("[ServerDB] Tables verified/created successfully.")


# ── WRITE OPERATIONS (used by web forms) ──────────────────────────────────

def upsert_visit(data: dict) -> bool:
    """Inserts visitor + visit log. Used by the web check-in form."""
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
            data["visitor_uuid"],
            data["full_name"],
            data.get("national_id"),
            data.get("phone_number"),
            data.get("vehicle_plate"),
            data["category"],
            bool(data.get("exception_flag", False)),
        ))

        cur.execute("""
            INSERT INTO visit_logs
                (local_uuid, visitor_uuid, guard_id, resident_id,
                 pax_count, estimated_minutes, check_in_time, check_out_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (local_uuid) DO UPDATE SET
                check_out_time    = EXCLUDED.check_out_time,
                pax_count         = EXCLUDED.pax_count,
                estimated_minutes = EXCLUDED.estimated_minutes
        """, (
            data["log_uuid"],
            data["visitor_uuid"],
            data.get("guard_id"),
            data.get("resident_id"),
            data.get("pax_count", 1),
            data.get("estimated_minutes"),
            data["check_in_time"],
            data.get("check_out_time"),
        ))

        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[ServerDB] upsert_visit error: {e}")
        return False


def upsert_passenger(log_uuid: str, national_id: str) -> bool:
    """Inserts one associated-passenger row. Used by web multi-pax check-in."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO associated_passengers (log_uuid, national_id)
            VALUES (%s, %s)
            ON CONFLICT (log_uuid, national_id) DO NOTHING
        """, (log_uuid, national_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[ServerDB] upsert_passenger error: {e}")
        return False


def web_checkout(log_uuid: str) -> bool:
    """Stamps check_out_time in Nairobi time. Returns True if a row was updated."""
    from datetime import datetime, timezone, timedelta
    eat = timezone(timedelta(hours=3))
    now_eat = datetime.now(eat).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE visit_logs
            SET check_out_time = %s
            WHERE local_uuid = %s AND check_out_time IS NULL
        """, (now_eat, log_uuid))
        conn.commit()
        rows = cur.rowcount
        cur.close()
        conn.close()
        return rows > 0
    except Exception as e:
        print(f"[ServerDB] web_checkout error: {e}")
        return False


# ── READ OPERATIONS ───────────────────────────────────────────────────────

def get_active_visits_server() -> list:
    """Visitors currently on premises (check_out_time IS NULL)."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                vl.local_uuid,
                v.full_name,
                v.category,
                v.vehicle_plate,
                vl.pax_count,
                vl.check_in_time,
                vl.estimated_minutes,
                v.exception_flag,
                COALESCE(STRING_AGG(ap.national_id, ' | '), '—') AS pax_ids
            FROM visit_logs vl
            JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            LEFT JOIN associated_passengers ap ON ap.log_uuid = vl.local_uuid
            WHERE vl.check_out_time IS NULL
            GROUP BY vl.local_uuid, v.full_name, v.category, v.vehicle_plate,
                     vl.pax_count, vl.check_in_time, vl.estimated_minutes,
                     v.exception_flag
            ORDER BY vl.check_in_time DESC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_active_visits error: {e}")
        return []


def get_visit_history_server(limit: int = 200) -> list:
    """Completed visits (check_out_time IS NOT NULL). Unfiltered."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                vl.local_uuid,
                v.full_name,
                v.national_id,
                v.category,
                vl.pax_count,
                vl.check_in_time,
                vl.check_out_time,
                v.exception_flag,
                COALESCE(STRING_AGG(ap.national_id, ' | '), '—') AS pax_ids
            FROM visit_logs vl
            JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            LEFT JOIN associated_passengers ap ON ap.log_uuid = vl.local_uuid
            WHERE vl.check_out_time IS NOT NULL
            GROUP BY vl.local_uuid, v.full_name, v.national_id, v.category,
                     vl.pax_count, vl.check_in_time, vl.check_out_time,
                     v.exception_flag
            ORDER BY vl.check_in_time DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_visit_history error: {e}")
        return []


def get_filtered_history(category: str = None,
                         date_from: str = None,
                         date_to: str = None,
                         limit: int = 200) -> list:
    """Reports page — completed visits with filters + was_overdue flag."""
    try:
        conn  = get_connection()
        cur   = conn.cursor()
        query = """
            SELECT
                vl.local_uuid,
                v.full_name,
                v.national_id,
                v.category,
                vl.pax_count,
                vl.check_in_time,
                vl.check_out_time,
                v.exception_flag,
                COALESCE(STRING_AGG(ap.national_id, ' | '), '—') AS pax_ids,
                CASE
                    WHEN v.category = 'Delivery'
                     AND EXTRACT(EPOCH FROM (
                         vl.check_out_time::timestamp -
                         vl.check_in_time::timestamp
                     )) / 60.0 > 20
                    THEN TRUE
                    ELSE FALSE
                END AS was_overdue
            FROM visit_logs vl
            JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            LEFT JOIN associated_passengers ap ON ap.log_uuid = vl.local_uuid
            WHERE vl.check_out_time IS NOT NULL
        """
        params = []
        if category:
            query += " AND v.category = %s"
            params.append(category)
        if date_from:
            query += " AND vl.check_in_time::date >= %s"
            params.append(date_from)
        if date_to:
            query += " AND vl.check_in_time::date <= %s"
            params.append(date_to)
        query += """
            GROUP BY vl.local_uuid, v.full_name, v.national_id, v.category,
                     vl.pax_count, vl.check_in_time, vl.check_out_time,
                     v.exception_flag
            ORDER BY vl.check_in_time DESC
            LIMIT %s
        """
        params.append(limit)
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_filtered_history error: {e}")
        return []


def get_stats_server() -> dict:
    """Counters for the top-of-page cards (total/today/active/by category)."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM visit_logs")
        total = cur.fetchone()["total"]
        cur.execute(
            "SELECT COUNT(*) AS today FROM visit_logs "
            "WHERE check_in_time::date = CURRENT_DATE"
        )
        today = cur.fetchone()["today"]
        cur.execute(
            "SELECT COUNT(*) AS active FROM visit_logs "
            "WHERE check_out_time IS NULL"
        )
        active = cur.fetchone()["active"]
        cur.execute("""
            SELECT v.category, COUNT(*) AS cnt
            FROM visit_logs vl
            JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            GROUP BY v.category
        """)
        by_cat = {row["category"]: row["cnt"] for row in cur.fetchall()}
        cur.close()
        conn.close()
        return {
            "total": total,
            "today": today,
            "active": active,
            "by_category": by_cat,
        }
    except Exception as e:
        print(f"[ServerDB] get_stats error: {e}")
        return {"total": 0, "today": 0, "active": 0, "by_category": {}}


# ── AUTH ──────────────────────────────────────────────────────────────────

def verify_guard_web(username: str, password: str):
    """Returns guard dict (with role) on valid credentials, else None."""
    hashed = hashlib.sha256(password.encode()).hexdigest()
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT guard_id, username, full_name, role
            FROM guards
            WHERE username  = %s
              AND password  = %s
              AND is_active = TRUE
        """, (username, hashed))
        guard = cur.fetchone()
        cur.close()
        conn.close()
        return dict(guard) if guard else None
    except Exception as e:
        print(f"[ServerDB] verify_guard_web error: {e}")
        return None


# ── GUARD MANAGEMENT (admin panel) ────────────────────────────────────────

def get_all_guards_server() -> list:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT guard_id, username, full_name, role, is_active, created_at
            FROM guards
            ORDER BY guard_id
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
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
        conn.close()
        return True
    except psycopg2.IntegrityError:
        return False
    except Exception as e:
        print(f"[ServerDB] add_guard error: {e}")
        return False


def toggle_guard_server(guard_id: int) -> bool:
    """Flips is_active. Returns the new is_active state."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE guards
            SET is_active = NOT is_active
            WHERE guard_id = %s
            RETURNING is_active
        """, (guard_id,))
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return bool(result["is_active"]) if result else False
    except Exception as e:
        print(f"[ServerDB] toggle_guard error: {e}")
        return False


def reset_guard_password_server(guard_id: int, new_password: str) -> bool:
    hashed = hashlib.sha256(new_password.encode()).hexdigest()
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE guards SET password = %s WHERE guard_id = %s",
            (hashed, guard_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[ServerDB] reset_guard_password error: {e}")
        return False


# ── RESIDENT MANAGEMENT (admin panel) ─────────────────────────────────────

def get_all_residents_server() -> list:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT resident_id, full_name, unit_number, host_pin, phone, is_active
            FROM residents
            ORDER BY resident_id
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ServerDB] get_all_residents error: {e}")
        return []


def add_resident_server(full_name: str, unit_number: str,
                        host_pin: str, phone: str) -> bool:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO residents (full_name, unit_number, host_pin, phone)
            VALUES (%s, %s, %s, %s)
        """, (full_name, unit_number, host_pin, phone or None))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except psycopg2.IntegrityError:
        return False
    except Exception as e:
        print(f"[ServerDB] add_resident error: {e}")
        return False


def update_resident_server(resident_id: int, full_name: str,
                           unit_number: str, phone: str) -> bool:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE residents
            SET full_name=%s, unit_number=%s, phone=%s
            WHERE resident_id=%s
        """, (full_name, unit_number, phone or None, resident_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[ServerDB] update_resident error: {e}")
        return False


def toggle_resident_server(resident_id: int) -> bool:
    """Flips is_active. Returns the new state."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE residents
            SET is_active = NOT is_active
            WHERE resident_id = %s
            RETURNING is_active
        """, (resident_id,))
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return bool(result["is_active"]) if result else False
    except Exception as e:
        print(f"[ServerDB] toggle_resident error: {e}")
        return False