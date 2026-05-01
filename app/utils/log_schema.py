from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


Level = Literal["DEBUG", "INFO", "WARN", "ERROR"]


class JobLog(BaseModel):
    ts: str
    level: Level = "INFO"
    jobId: str
    keyword: str
    step: str = ""
    message: str
    data: Optional[dict[str, Any]] = None

    # For pagination convenience
    seq: int = Field(default=0, description="Monotonic sequence id in DB")

