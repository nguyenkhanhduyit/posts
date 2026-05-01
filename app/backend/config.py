from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.utils.paths import repo_root


@dataclass(frozen=True)
class Settings:
    backend_host: str
    backend_port: int
    sqlite_path: Path
    chrome_profile_dir: Path

    default_max_posts: int
    default_delay_min_sec: float
    default_delay_max_sec: float
    default_between_keywords_delay_min_sec: float
    default_between_keywords_delay_max_sec: float
    default_max_keywords: int
    default_worker_count: int


def load_settings() -> Settings:
    load_dotenv(repo_root() / "app" / ".env")

    root = repo_root()
    sqlite_rel = os.getenv("SQLITE_PATH", "app/storage/app.db")
    profile_rel = os.getenv("CHROME_PROFILE_DIR", "app/chrome-profile")

    return Settings(
        backend_host=os.getenv("BACKEND_HOST", "127.0.0.1"),
        backend_port=int(os.getenv("BACKEND_PORT", "8080")),
        sqlite_path=(root / sqlite_rel).resolve(),
        chrome_profile_dir=(root / profile_rel).resolve(),
        default_max_posts=int(os.getenv("DEFAULT_MAX_POSTS", "15")),
        default_delay_min_sec=float(os.getenv("DEFAULT_DELAY_MIN_SEC", "1")),
        default_delay_max_sec=float(os.getenv("DEFAULT_DELAY_MAX_SEC", "3")),
        default_between_keywords_delay_min_sec=float(
            os.getenv("DEFAULT_BETWEEN_KEYWORDS_DELAY_MIN_SEC", "1")
        ),
        default_between_keywords_delay_max_sec=float(
            os.getenv("DEFAULT_BETWEEN_KEYWORDS_DELAY_MAX_SEC", "2")
        ),
        default_max_keywords=int(os.getenv("DEFAULT_MAX_KEYWORDS", "500")),
        default_worker_count=int(os.getenv("DEFAULT_WORKER_COUNT", "1")),
    )

