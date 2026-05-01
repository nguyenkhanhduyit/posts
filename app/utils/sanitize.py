from __future__ import annotations

import re


_INVALID_WIN = r'<>:"/\\|?*\x00-\x1F'
_INVALID_RE = re.compile(f"[{re.escape(_INVALID_WIN)}]")


def sanitize_keyword_for_path(keyword: str, max_len: int = 80) -> str:
    k = (keyword or "").strip()
    k = _INVALID_RE.sub("_", k)
    k = re.sub(r"\s+", " ", k).strip()
    if not k:
        return "keyword"
    if len(k) > max_len:
        k = k[:max_len].rstrip()
    return k

