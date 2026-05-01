from __future__ import annotations

import json
import sqlite3
import uuid
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from app.utils.timeutil import utc_now_iso


@dataclass(frozen=True)
class JobRow:
    id: str
    keyword: str
    email: str
    password_enc: str
    max_posts: int
    delay_min_sec: float
    delay_max_sec: float
    between_keywords_delay_min_sec: float
    between_keywords_delay_max_sec: float
    status: str
    progress_current: int
    progress_total: int
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    last_error: Optional[str]
    cancel_requested: int
    attempt: int
    max_attempts: int
    headless: int
    checkpoint_pending: int = 0
    checkpoint_message: Optional[str] = None
    checkpoint_decision: Optional[str] = None
    retry_worker_id: Optional[str] = None
    last_worker_id: Optional[str] = None
    assigned_worker_id: Optional[str] = None


def _row_to_job(r: sqlite3.Row) -> JobRow:
    d = dict(r)
    # Backward/forward compatibility:
    # The DB may contain extra columns from older versions (e.g. save_html).
    # JobRow should ignore unknown keys to avoid crashing workers.
    try:
        d.pop("save_html", None)
    except Exception:
        pass
    d.setdefault("retry_worker_id", None)
    d.setdefault("last_worker_id", None)
    d.setdefault("assigned_worker_id", None)
    d.setdefault("checkpoint_pending", 0)
    d.setdefault("checkpoint_message", None)
    d.setdefault("checkpoint_decision", None)
    return JobRow(**d)


class JobRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_jobs(
        self,
        *,
        email: str,
        password_enc: str,
        keywords: list[str],
        assigned_worker_ids: Optional[list[int]] = None,
        headless: bool,
        max_posts: int,
        delay_min_sec: float,
        delay_max_sec: float,
        between_keywords_delay_min_sec: float,
        between_keywords_delay_max_sec: float,
        max_attempts: int = 1,
    ) -> list[str]:
        ids: list[str] = []
        with self.conn:
            for i, kw in enumerate(keywords):
                # Use per-row timestamp to keep a stable order even when jobs are created in a tight loop.
                now = utc_now_iso()
                job_id = str(uuid.uuid4())
                aw = None
                try:
                    if assigned_worker_ids is not None and i < len(assigned_worker_ids):
                        aw = str(int(assigned_worker_ids[i]))
                except Exception:
                    aw = None
                self.conn.execute(
                    """
                    INSERT INTO jobs(
                      id, keyword, email, password_enc,
                      headless,
                      max_posts, delay_min_sec, delay_max_sec,
                      between_keywords_delay_min_sec, between_keywords_delay_max_sec,
                      status, progress_current, progress_total,
                      created_at, started_at, finished_at,
                      last_error, cancel_requested, attempt, max_attempts,
                      assigned_worker_id
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, NULL, NULL, NULL, 0, 0, ?, ?);
                    """,
                    (
                        job_id,
                        kw,
                        email,
                        password_enc,
                        1 if headless else 0,
                        max_posts,
                        delay_min_sec,
                        delay_max_sec,
                        between_keywords_delay_min_sec,
                        between_keywords_delay_max_sec,
                        max_posts,
                        now,
                        max_attempts,
                        aw,
                    ),
                )
                ids.append(job_id)
        return ids

    def list_jobs(self) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT
              id, keyword, status,
              progress_current, progress_total,
              created_at, started_at, finished_at,
              last_error, last_worker_id, retry_worker_id, assigned_worker_id,
              checkpoint_pending, checkpoint_message
            FROM jobs
            ORDER BY created_at DESC, rowid DESC;
            """
        )
        return [dict(r) for r in cur.fetchall()]

    def mark_checkpoint_pending(self, job_id: str, message: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE jobs
                SET checkpoint_pending=1, checkpoint_message=?, checkpoint_decision=NULL
                WHERE id=?;
                """,
                (str(message or "")[:2000], job_id),
            )

    def clear_checkpoint(self, job_id: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE jobs
                SET checkpoint_pending=0, checkpoint_message=NULL, checkpoint_decision=NULL
                WHERE id=?;
                """,
                (job_id,),
            )

    def set_checkpoint_decision(self, job_id: str, decision: str) -> None:
        # decision: "reload" | "continue"
        d = str(decision or "").strip().lower()
        if d not in {"reload", "continue"}:
            d = "continue"
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET checkpoint_decision=? WHERE id=?;",
                (d, job_id),
            )

    def get_checkpoint_state(self, job_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT checkpoint_pending, checkpoint_message, checkpoint_decision FROM jobs WHERE id=?;",
            (job_id,),
        ).fetchone()
        if not row:
            return {"pending": 0, "message": None, "decision": None}
        return {
            "pending": int(row["checkpoint_pending"] or 0),
            "message": row["checkpoint_message"],
            "decision": row["checkpoint_decision"],
        }

    def get_job(self, job_id: str) -> Optional[JobRow]:
        cur = self.conn.execute("SELECT * FROM jobs WHERE id = ?;", (job_id,))
        r = cur.fetchone()
        return _row_to_job(r) if r else None

    def request_cancel(self, job_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE jobs SET cancel_requested = 1 WHERE id = ?;", (job_id,)
            )
            return cur.rowcount > 0

    def request_cancel_all(self) -> int:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE jobs SET cancel_requested = 1 WHERE status IN ('pending','running');"
            )
            return int(cur.rowcount)

    def claim_next_pending(self, worker_id: int = 0) -> Optional[JobRow]:
        """
        Atomically claim one pending job and mark it running.
        Jobs reset for retry with retry_worker_id set are preferred by that same worker
        so CAPTCHA/timeout retries do not "jump" to another keyword on relaunch.
        """
        now = utc_now_iso()
        wid = str(int(worker_id))
        # Severe-stall fix:
        # Old versions hard-assigned jobs to workers (assigned_worker_id=i%workerCount).
        # If worker_count changes, a worker crashes, or assignments are uneven, the queue can stall:
        # pending jobs exist but no eligible worker claims them.
        #
        # Default: allow stealing assigned jobs so the system always progresses.
        # Set ALLOW_STEAL_ASSIGNED_JOBS=0 to restore strict assignment behavior.
        allow_steal = (os.getenv("ALLOW_STEAL_ASSIGNED_JOBS", "1") or "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        with self.conn:
            if allow_steal:
                row = self.conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'pending' AND cancel_requested = 0
                      AND (retry_worker_id IS NULL OR retry_worker_id = ?)
                    ORDER BY
                      CASE WHEN retry_worker_id = ? THEN 0 ELSE 1 END,
                      created_at ASC,
                      rowid ASC
                    LIMIT 1;
                    """,
                    (wid, wid),
                ).fetchone()
            else:
                row = self.conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'pending' AND cancel_requested = 0
                      AND (retry_worker_id IS NULL OR retry_worker_id = ?)
                      AND (assigned_worker_id IS NULL OR assigned_worker_id = ? OR retry_worker_id = ?)
                    ORDER BY
                      CASE WHEN retry_worker_id = ? THEN 0 ELSE 1 END,
                      created_at ASC,
                      rowid ASC
                    LIMIT 1;
                    """,
                    (wid, wid, wid, wid),
                ).fetchone()
            if not row:
                return None
            job_id = row["id"]
            cur = self.conn.execute(
                """
                UPDATE jobs
                SET status='running', started_at=?, last_worker_id=?
                WHERE id=? AND status='pending';
                """,
                (now, wid, job_id),
            )
            # IMPORTANT: avoid race where 2 workers select same pending row.
            # If we didn't actually update (rowcount==0), someone else claimed it.
            if not cur or int(getattr(cur, "rowcount", 0) or 0) <= 0:
                return None
        return self.get_job(job_id)

    def mark_progress(self, job_id: str, current: int, total: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET progress_current=?, progress_total=? WHERE id=?;",
                (current, total, job_id),
            )

    def mark_done(self, job_id: str) -> None:
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status='done', finished_at=?, retry_worker_id=NULL WHERE id=?;",
                (now, job_id),
            )

    def mark_cancelled(self, job_id: str) -> None:
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status='cancelled', finished_at=?, retry_worker_id=NULL WHERE id=?;",
                (now, job_id),
            )

    def mark_error(self, job_id: str, message: str) -> None:
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE jobs
                SET status='error', finished_at=?, last_error=?, retry_worker_id=NULL
                WHERE id=?;
                """,
                (now, message[:2000], job_id),
            )

    def bump_attempt(self, job_id: str) -> JobRow:
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET attempt = attempt + 1 WHERE id=?;",
                (job_id,),
            )
        j = self.get_job(job_id)
        if not j:
            raise RuntimeError("Job not found after bump_attempt")
        return j

    def reset_to_pending_for_retry(self, job_id: str, err: str, *, worker_id: int = 0) -> None:
        wid = str(int(worker_id))
        with self.conn:
            self.conn.execute(
                """
                UPDATE jobs
                SET status='pending', started_at=NULL, finished_at=NULL, last_error=?,
                    retry_worker_id=?
                WHERE id=?;
                """,
                (err[:2000], wid, job_id),
            )

    def is_cancel_requested(self, job_id: str) -> bool:
        row = self.conn.execute(
            "SELECT cancel_requested FROM jobs WHERE id=?;", (job_id,)
        ).fetchone()
        return bool(row and int(row["cancel_requested"]) == 1)

    def delete_all(self) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM jobs;")
            return int(cur.rowcount)


class LogRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def append(
        self,
        *,
        job_id: str,
        ts: str,
        level: str,
        keyword: str,
        step: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> int:
        data_json = json.dumps(data, ensure_ascii=False) if data is not None else None
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO job_logs(job_id, ts, level, keyword, step, message, data_json)
                VALUES(?, ?, ?, ?, ?, ?, ?);
                """,
                (job_id, ts, level, keyword, step, message, data_json),
            )
            return int(cur.lastrowid)

    def list_after(self, job_id: str, offset_seq: int, limit: int = 300) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT seq, job_id, ts, level, keyword, step, message, data_json
            FROM job_logs
            WHERE job_id=? AND seq > ?
            ORDER BY seq ASC
            LIMIT ?;
            """,
            (job_id, offset_seq, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def delete_all(self) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM job_logs;")
            return int(cur.rowcount)

