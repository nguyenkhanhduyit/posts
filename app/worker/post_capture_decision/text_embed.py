"""
Semantic embeddings via remote APIs are DISABLED — project uses local keyword heuristics only.

Keeping module for stable imports (`blend_text_scores`, `semantic_keyword_body_similarity`).
"""

from __future__ import annotations

from app.worker.ai_inference_policy import REMOTE_CLOUD_AI_ENABLED


def semantic_keyword_body_similarity(keyword: str, body: str) -> tuple[float | None, str]:
    _ = keyword, body
    if REMOTE_CLOUD_AI_ENABLED:
        return None, "remote_embed_not_bundled_set_REMOTE_CLOUD_AI_and_implement_locally"
    return None, "local_only_policy_no_remote_embeddings"


def blend_text_scores(heuristic_score: float, embed_score: float | None, blend: float) -> tuple[float, str]:
    """If no embed, returns heuristic unchanged."""
    if embed_score is None:
        return float(heuristic_score), "heuristic_only"
    _ = blend
    b = max(0.0, min(1.0, float(blend)))
    out = (1.0 - b) * float(heuristic_score) + b * float(embed_score)
    return max(0.0, min(1.0, out)), f"blend_{b:.2f}"
