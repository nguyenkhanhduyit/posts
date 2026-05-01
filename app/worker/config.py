from __future__ import annotations

from app.backend.config import Settings, load_settings


def worker_settings() -> Settings:
    # Reuse backend settings (.env)
    return load_settings()

