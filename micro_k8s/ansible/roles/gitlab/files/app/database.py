"""
SQLite database initialisation, migrations, and helper utilities.
"""
import os
import sqlite3
from contextlib import contextmanager

from app.config import DB_PATH, logger


@contextmanager
def get_db():
    """Context manager that yields a (conn, cursor) pair and auto-commits/closes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        c = conn.cursor()
        yield conn, c
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create schema and run migrations.  Safe to call on every startup."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    # ------------------------------------------------------------------
    # Table: quota_groups
    # ------------------------------------------------------------------
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_groups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL UNIQUE,
            description     TEXT,
            cpu_limit       REAL    NOT NULL,   -- cores, e.g. 4.0
            memory_limit    TEXT    NOT NULL,   -- e.g. "16Gi"
            storage_limit   TEXT    NOT NULL,   -- e.g. "50Gi"
            gpu_limit       INTEGER NOT NULL DEFAULT 0,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # ------------------------------------------------------------------
    # Table: users  (original schema)
    # ------------------------------------------------------------------
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            gitlab_id       INTEGER UNIQUE,
            name            TEXT,
            username        TEXT UNIQUE,
            email           TEXT UNIQUE,
            password        TEXT,
            profile_status  TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # ------------------------------------------------------------------
    # Migration: add quota_group_id FK to users (idempotent)
    # ------------------------------------------------------------------
    existing_cols = {row[1] for row in c.execute("PRAGMA table_info(users)")}
    if "quota_group_id" not in existing_cols:
        logger.info("Migration: adding quota_group_id column to users table")
        c.execute(
            """
            ALTER TABLE users
            ADD COLUMN quota_group_id INTEGER
                REFERENCES quota_groups(id) ON DELETE SET NULL
            """
        )

    conn.commit()
    conn.close()


def update_profile_status(username: str, status: str) -> None:
    """Update the profile_status column for a given username."""
    try:
        with get_db() as (conn, c):
            c.execute(
                "UPDATE users SET profile_status = ? WHERE username = ?",
                (status, username),
            )
    except Exception as exc:
        logger.error(f"Failed to update SQLite profile status: {exc}")
