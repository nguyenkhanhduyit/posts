"""
Fast text-side gates for “should we keep this post screenshot?” — no ML deps.

Uses DOM-extracted text + search keyword; optional sponsored / junk markers.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass


_SPONSORED_RE = re.compile(
    r"(được\s*tài\s*trợ|tài\s*trợ|sponsored|paid\s*partnership|quảng\s*cáo|ad\s*choices|"
    r"promoted|đề\s*xuất\s*bài\s*viết)",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    t = unicodedata.normalize("NFKC", s or "")
    t = t.lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _collapse_alnum(s: str) -> str:
    return re.sub(r"[^\w\u00C0-\u1EF9]+", "", (s or "").lower())


@dataclass(frozen=True)
class TextGateResult:
    """pass_gate=True means text arm does not veto this capture."""

    pass_gate: bool
    score: float  # 0..1 relevance / confidence for fusion
    reason: str
    keyword_token_hits: int = 0
    keyword_token_total: int = 0


def analyze_post_text_for_capture(
    *,
    search_keyword: str,
    post_body: str,
    min_keyword_coverage: float | None = None,
    reject_sponsored: bool | None = None,
) -> TextGateResult:
    """
    - Keyword empty → pass (no text constraint).
    - Strong sponsored markers → fail if reject_sponsored.
    - Otherwise score = fraction of significant keyword tokens found in body.
    """
    mk = _norm(search_keyword)
    body = _norm(post_body)
    if body == "":
        return TextGateResult(
            pass_gate=True,
            score=1.0,
            reason="empty_body_skip",
        )

    rs = reject_sponsored
    if rs is None:
        rs = os.getenv("POST_CAPTURE_REJECT_SPONSORED_TEXT", "1").strip().lower() in {"1", "true", "yes", "on"}
    if rs and _SPONSORED_RE.search(body):
        return TextGateResult(pass_gate=False, score=0.0, reason="sponsored_or_ad_marker")

    if mk == "":
        return TextGateResult(pass_gate=True, score=1.0, reason="no_search_keyword")

    # Whole-keyword substring (good for Vietnamese phrases without tokenization).
    if mk in body or _collapse_alnum(mk) in _collapse_alnum(body):
        return TextGateResult(
            pass_gate=True,
            score=1.0,
            reason="keyword_phrase_match",
            keyword_token_total=1,
            keyword_token_hits=1,
        )

    raw_tokens = [t for t in re.split(r"[\s,.;:!?]+", mk) if len(t) >= 2][:14]
    if not raw_tokens:
        return TextGateResult(pass_gate=True, score=1.0, reason="no_keyword_tokens")

    hits = sum(1 for t in raw_tokens if t in body)
    total = len(raw_tokens)
    cov = float(hits) / float(max(1, total))

    mink = min_keyword_coverage
    if mink is None:
        try:
            mink = float(os.getenv("POST_CAPTURE_TEXT_MIN_COVERAGE", "0.34") or "0.34")
        except Exception:
            mink = 0.34
    mink = max(0.0, min(1.0, float(mink)))

    # At least one token hit required when keyword has multiple words; single token must hit.
    if total == 1:
        ok = hits >= 1
    else:
        ok = cov >= mink or hits >= max(1, (total + 1) // 2)

    sc = max(0.0, min(1.0, cov))
    return TextGateResult(
        pass_gate=bool(ok),
        score=float(sc),
        reason="keyword_token_coverage",
        keyword_token_hits=int(hits),
        keyword_token_total=int(total),
    )
