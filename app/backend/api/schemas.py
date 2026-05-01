from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class StartJobRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)


class StartJobResponse(BaseModel):
    jobIds: list[str]


class StopRequest(BaseModel):
    jobId: Optional[str] = None
    all: bool = False


class StopResponse(BaseModel):
    ok: bool
    cancelled: int = 0

