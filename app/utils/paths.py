from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    # app/utils/paths.py -> utils -> app -> repo root
    return Path(__file__).resolve().parents[2]


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p

