from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_date_yyyy_mm_dd() -> str:
    return datetime.now().strftime("%Y-%m-%d")

