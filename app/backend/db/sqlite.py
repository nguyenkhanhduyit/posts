from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Increase SQLite busy timeout to reduce "database is locked" under multi-process workers.
    # This does not make operations infinite; it just waits briefly for the writer lock.
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY,
          keyword TEXT NOT NULL,
          email TEXT NOT NULL,
          password_enc TEXT NOT NULL,
          max_posts INTEGER NOT NULL,
          delay_min_sec REAL NOT NULL,
          delay_max_sec REAL NOT NULL,
          between_keywords_delay_min_sec REAL NOT NULL,
          between_keywords_delay_max_sec REAL NOT NULL,
          status TEXT NOT NULL,
          progress_current INTEGER NOT NULL DEFAULT 0,
          progress_total INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT,
          last_error TEXT,
          cancel_requested INTEGER NOT NULL DEFAULT 0,
          attempt INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 2,
          headless INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    # Lightweight migrations (SQLite doesn't support IF NOT EXISTS for columns)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs);").fetchall()}
    if "headless" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN headless INTEGER NOT NULL DEFAULT 0;")
    if "retry_worker_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN retry_worker_id TEXT;")
    if "last_worker_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN last_worker_id TEXT;")
    if "assigned_worker_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN assigned_worker_id TEXT;")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_logs (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL,
          ts TEXT NOT NULL,
          level TEXT NOT NULL,
          keyword TEXT NOT NULL,
          step TEXT,
          message TEXT NOT NULL,
          data_json TEXT
        );
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_logs_job_seq
        ON job_logs(job_id, seq);
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )

    conn.commit()

