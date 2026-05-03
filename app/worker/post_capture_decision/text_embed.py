"""
Optional OpenAI text embeddings for keyword ↔ post body similarity (no extra deps).
Falls back silently when disabled or missing key/network error.
"""

from __future__ import annotations

import json
import math
import os
import ssl
import urllib.request
from typing import Any


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return dot / (na * nb)


def _embedding_openai(text: str, model: str, api_key: str, timeout_sec: float) -> list[float] | None:
    t = str(text or "").strip()
    if not t:
        return None
    if len(t) > 7800:
        t = t[:7800]
    url = str(os.getenv("POST_CAPTURE_EMBED_URL") or "https://api.openai.com/v1/embeddings").strip()
    body = json.dumps({"model": model, "input": t}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        jd = json.loads(raw)
        data = jd.get("data") or []
        if not data:
            return None
        vec = data[0].get("embedding") if isinstance(data[0], dict) else None
        if isinstance(vec, list) and vec and isinstance(vec[0], (int, float)):
            return [float(x) for x in vec]
    except Exception:
        return None
    return None


def semantic_keyword_body_similarity(keyword: str, body: str) -> tuple[float | None, str]:
    """
    Returns (similarity 0..1-ish from cosine centered, None if skipped) and reason token.
    Cosine typically 0..1 for similar intents; clamp and map mildly for gate score use.
    """
    if not _env_bool("POST_CAPTURE_TEXT_EMBED", default=False):
        return None, "embed_disabled"
    key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        return None, "no_OPENAI_API_KEY"
    kw = str(keyword or "").strip()
    bd = str(body or "").strip()
    if len(bd) < 8:
        return None, "body_too_short"
    if not kw:
        return None, "empty_keyword"

    model = str(os.getenv("POST_CAPTURE_EMBED_MODEL") or "text-embedding-3-small").strip()
    tout = float(os.getenv("POST_CAPTURE_EMBED_TIMEOUT_SEC", "12") or "12")

    ek = _embedding_openai(kw, model, key, tout)
    eb = _embedding_openai(bd[:8000], model, key, tout)
    if ek is None or eb is None:
        return None, "embed_api_fail"

    c = _cosine_sim(ek, eb)
    # Map cosine [-1,1] -> [0,1] style score for gates
    mapped = float((c + 1.0) / 2.0)
    return max(0.0, min(1.0, mapped)), "ok"


def blend_text_scores(heuristic_score: float, embed_score: float | None, blend: float) -> tuple[float, str]:
    """
    blend ∈ [0,1]: fraction of semantic vs heuristic (0=all heuristic).
    """
    if embed_score is None:
        return float(heuristic_score), "heuristic_only"
    b = max(0.0, min(1.0, float(blend)))
    out = (1.0 - b) * float(heuristic_score) + b * float(embed_score)
    return max(0.0, min(1.0, out)), f"blend_{b:.2f}"
