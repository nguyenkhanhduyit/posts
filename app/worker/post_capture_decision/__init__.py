"""Multi-signal post capture decisions (text / image / fusion)."""

from app.worker.post_capture_decision.decision import (
    CaptureGateDecision,
    attempt_vlm_rescue_after_reject,
    decide_post_capture,
    mode_needs_media_struct,
    mode_uses_dom_text,
    public_config_snapshot,
)
from app.worker.post_capture_decision.text_heuristics import TextGateResult, analyze_post_text_for_capture

__all__ = [
    "CaptureGateDecision",
    "TextGateResult",
    "analyze_post_text_for_capture",
    "attempt_vlm_rescue_after_reject",
    "decide_post_capture",
    "mode_needs_media_struct",
    "mode_uses_dom_text",
    "public_config_snapshot",
]
