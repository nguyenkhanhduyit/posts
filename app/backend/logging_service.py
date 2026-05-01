from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.utils.paths import ensure_dir
from app.utils.timeutil import local_date_yyyy_mm_dd, utc_now_iso

from app.backend.queue.repo import LogRepo


@dataclass(frozen=True)
class LogPaths:
    logs_root: Path

    def file_for_job(self, job_id: str) -> Path:
        day = local_date_yyyy_mm_dd()
        return self.logs_root / day / f"{job_id}.log"


class JobLogger:
    def __init__(self, log_repo: LogRepo, paths: LogPaths):
        self.log_repo = log_repo
        self.paths = paths

    def _append_file(self, path: Path, line: str) -> None:
        ensure_dir(path.parent)
        path.open("a", encoding="utf-8").write(line + "\n")

    def log(
        self,
        *,
        job_id: str,
        keyword: str,
        level: str,
        step: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> int:
        ts = utc_now_iso()
        seq = self.log_repo.append(
            job_id=job_id,
            ts=ts,
            level=level,
            keyword=keyword,
            step=step,
            message=message,
            data=data,
        )
        payload = {
            "ts": ts,
            "level": level,
            "jobId": job_id,
            "keyword": keyword,
            "step": step,
            "message": message,
            "data": data,
            "seq": seq,
        }
        self._append_file(self.paths.file_for_job(job_id), json.dumps(payload, ensure_ascii=False))
        return seq

