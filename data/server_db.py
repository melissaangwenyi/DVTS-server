"""
=============================================================================
server/data/server_db.py  —  Server-Side Database (PostgreSQL)
Location: server/data/server_db.py

PURPOSE:
    Handles all database operations on the SERVER side.
    Uses PostgreSQL instead of SQLite because PostgreSQL:
      - Handles many computers connecting at the same time (concurrent access)
      - Lives on a proper server with backups
      - Is the industry standard for web applications

WHAT IS POSTGRESQL vs SQLITE:
    SQLite  = a single file on the guard's computer. One user at a time.
    PostgreSQL = a full database server. Many users simultaneously.
                 Lives in the cloud (Railway handles this for you).

HOW THE CONNECTION WORKS:
    The DATABASE_URL environment variable is set by Railway automatically
    when you add a PostgreSQL database to your project.
    It looks like: postgresql://user:password@host:5432/dbname
    We never hardcode passwords — we read them from the environment.

psycopg2 explained:
    psycopg2 is the Python library for talking to PostgreSQL.
    It works almost identically to sqlite3 — same .execute(), .fetchone(),
    .fetchall() — just connecting to a server instead of a file.
=============================================================================
"""

import os
import psycopg2
import psycopg2.extras   # gives us dictionary-style row access

# Railway sets this environment variable automatically.
# When testing locally, set it yourself in your terminal:
#   export DATABASE_URL="postgresql://localhost/vts_test"
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_connection():
    """
    Opens and returns a PostgreSQL connection.
    psycopg2.extras.RealDictCursor means rows come back as dictionaries
    (row["full_name"]) instead of tuples (row[0]) — same as sqlite3.Row.
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set.\n"
            "On Railway: add a PostgreSQL database to your project.\n"
            "Locally: export DATABASE_URL='postgresql://localhost/vts'"
        )
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_server_db():
    """
    Creates all tables on the PostgreSQL server if they don't exist yet.
    Called once when the Flask server starts up (in server/app.py).

    The schema mirrors the local SQLite schema but uses PostgreSQL syntax:
      - SERIAL instead of INTEGER PRIMARY KEY AUTOINCREMENT
      - TEXT columns work the same
      - BOOLEAN instead of INTEGER for flags
      - NOW() instead of datetime('now','localtime') — PostgreSQL handles
        timezone via the TIMESTAMPTZ type
    """
    conn = get_connection()
    cur  = conn.cursor()

    # Guards table — guard accounts synced from desktop
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guards (
            guard_id   SERIAL PRIMARY KEY,
            username   TEXT   NOT NULL UNIQUE,
            full_name  TEXT   NOT NULL,
            is_active  BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # Residents table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS residents (
            resident_id INTEGER PRIMARY KEY,
            full_name   TEXT NOT NULL,
            unit_number TEXT NOT NULL,
            host_pin    TEXT NOT NULL UNIQUE,
            phone       TEXT,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

    # Visitors table — uses local_uuid as the true primary key for sync
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

    # Visit logs — core transaction table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visit_logs (
            local_uuid      TEXT    PRIMARY KEY,
            visitor_uuid    TEXT    NOT NULL REFERENCES visitors(local_uuid),
            guard_id        INTEGER,
            resident_id     INTEGER,
            pax_count       INTEGER NOT NULL DEFAULT 1,
            check_in_time   TEXT    NOT NULL,
            check_out_time  TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # Associated passengers
    cur.execute("""
        CREATE TABLE IF NOT EXISTS associated_passengers (
            id          SERIAL PRIMARY KEY,
            log_uuid    TEXT NOT NULL REFERENCES visit_logs(local_uuid),
            national_id TEXT NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("[ServerDB] Tables verified/created successfully.")


# ── SYNC WRITE OPERATIONS ──────────────────────────────────────────────────
# These are called by the Flask API routes when data arrives from the desktop.

def upsert_visit(data: dict) -> bool:
    """
    Saves a visit record sent from the desktop app.
    'upsert' = INSERT if new, UPDATE if already exists (by UUID).

    This handles the duplicate scenario safely:
    If the desktop sends the same record twice (e.g. a retry after a
    network blip), the ON CONFLICT clause updates instead of crashing.
    The UUID is the key — same UUID = same record.
    """
    try:
        conn = get_connection()
        cur  = conn.cursor()

        # Save visitor profile first (the parent record)
        cur.execute("""
            INSERT INTO visitors
                (local_uuid, full_name, national_id, phone_number,
                 vehicle_plate, category, exception_flag, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (local_uuid) DO UPDATE SET
                full_name     = EXCLUDED.full_name,
                national_id   = EXCLUDED.national_id,
                phone_number  = EXCLUDED.phone_number,
                vehicle_plate = EXCLUDED.vehicle_plate,
                category      = EXCLUDED.category,
                exception_flag= EXCLUDED.exception_flag
        """, (
            data["visitor_uuid"],
            data["full_name"],
            data.get("national_id"),
            data.get("phone_number"),
            data.get("vehicle_plate"),
            data["category"],
            bool(data.get("exception_flag", False)),
        ))

        # Save the visit log (child record — references visitor via UUID)
        cur.execute("""
            INSERT INTO visit_logs
                (local_uuid, visitor_uuid, guard_id, resident_id,
                 pax_count, check_in_time, check_out_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (local_uuid) DO UPDATE SET
                check_out_time = EXCLUDED.check_out_time,
                pax_count      = EXCLUDED.pax_count
        """, (
            data["log_uuid"],
            data["visitor_uuid"],
            data.get("guard_id"),
            data.get("resident_id"),
            data.get("pax_count", 1),
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


def upsert_checkout(log_uuid: str, check_out_time: str) -> bool:
    """
    Updates the check_out_time for a visit that was already in the database.
    Called when a checkout happens offline and syncs later.
    """
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE visit_logs
            SET check_out_time = %s
            WHERE local_uuid = %s
        """, (check_out_time, log_uuid))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[ServerDB] upsert_checkout error: {e}")
        return False


def upsert_passenger(log_uuid: str, national_id: str) -> bool:
    """
    Saves a passenger record.
    The DO NOTHING on conflict means if we accidentally send the same
    passenger twice, we just ignore the second one quietly.
    """
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO associated_passengers (log_uuid, national_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
        """, (log_uuid, national_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[ServerDB] upsert_passenger error: {e}")
        return False


# ── READ OPERATIONS (for web dashboard) ───────────────────────────────────

def get_active_visits_server() -> list:
    """Returns all currently active visits from PostgreSQL."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                vl.local_uuid, v.full_name, v.category,
                v.vehicle_plate, vl.pax_count, vl.check_in_time,
                v.exception_flag
            FROM visit_logs vl
            JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            WHERE vl.check_out_time IS NULL
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
    """Returns completed visits from PostgreSQL for the web dashboard."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                vl.local_uuid, v.full_name, v.national_id,
                v.category, vl.pax_count,
                vl.check_in_time, vl.check_out_time,
                v.exception_flag
            FROM visit_logs vl
            JOIN visitors v ON v.local_uuid = vl.visitor_uuid
            WHERE vl.check_out_time IS NOT NULL
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


def get_stats_server() -> dict:
    """Summary statistics for the web dashboard."""
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
        cur.close()
        conn.close()
        return {"total": total, "today": today, "active": active}
    except Exception as e:
        print(f"[ServerDB] get_stats error: {e}")
        return {"total": 0, "today": 0, "active": 0}