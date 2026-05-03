"""
Multi-signal fusion for post screenshot keep/reject.

Modes: image_only | text_only | both | weighted

Optional: OpenAI text embeddings (POST_CAPTURE_TEXT_EMBED), DOM media requirement,
VLM rescue after reject (POST_CAPTURE_VLM_RESCUE).
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from app.worker.post_capture_decision.media_gate import dom_media_hard_veto, public_media_config
from app.worker.post_capture_decision.text_embed import blend_text_scores, semantic_keyword_body_similarity
from app.worker.post_capture_decision.text_heuristics import TextGateResult, analyze_post_text_for_capture

Mode = Literal["image_only", "text_only", "both", "weighted"]


def mode_uses_dom_text() -> bool:
    return _env_mode() in {"text_only", "both", "weighted"}


def mode_needs_media_struct() -> bool:
    return _env_mode() in {"text_only", "both", "weighted"} and (
        os.getenv("POST_CAPTURE_REQUIRE_MEDIA", "").strip().lower() in {"1", "true", "yes", "on"}
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or str(default))
    except Exception:
        return default


def _env_mode() -> Mode:
    raw = (os.getenv("POST_CAPTURE_MODE") or "image_only").strip().lower()
    if raw in {"text", "text_only"}:
        return "text_only"
    if raw in {"image", "image_only"}:
        return "image_only"
    if raw in {"both", "and", "all"}:
        return "both"
    if raw in {"weighted", "blend", "fusion"}:
        return "weighted"
    return "image_only"


def _image_discards(res: dict[str, Any] | None) -> bool:
    if res is None or not res.get("ok"):
        return False
    return res.get("is_positive") is False


def _image_keep_confidence(res: dict[str, Any] | None) -> float:
    if res is None or not res.get("ok"):
        return 0.55
    if res.get("is_positive") is True:
        return 1.0
    if res.get("is_positive") is False:
        return 0.0
    return 0.55


def _enhance_text_with_optional_embedding(keyword: str, body: str, base: TextGateResult) -> TextGateResult:
    sem, sr = semantic_keyword_body_similarity(keyword, body)
    if sem is None:
        return base
    blend_w = _env_float("POST_CAPTURE_EMBED_BLEND", 0.35)
    bl_score, mix = blend_text_scores(base.score, sem, blend_w)
    sem_pass_floor = _env_float("POST_CAPTURE_EMBED_PASS_MIN", 0.62)
    new_pass = bool(base.pass_gate or float(sem) >= sem_pass_floor)
    reason = f"{base.reason}+emb[{mix}|{sr}|sem={sem:.2f}]"
    return replace(base, score=bl_score, pass_gate=new_pass, reason=reason)


class CaptureGateDecision:
    __slots__ = ("discard", "mode", "text", "image_discard_raw", "summary", "trace")

    def __init__(
        self,
        discard: bool,
        mode: Mode,
        text: TextGateResult | None,
        image_discard_raw: bool,
        summary: str,
        trace: dict[str, Any],
    ) -> None:
        self.discard = bool(discard)
        self.mode = mode
        self.text = text
        self.image_discard_raw = bool(image_discard_raw)
        self.summary = str(summary)
        self.trace = trace


def decide_post_capture(
    *,
    search_keyword: str,
    dom_post_text: str,
    classifier_result: dict[str, Any] | None,
    ai_enabled: bool,
    dom_media: dict[str, Any] | None = None,
) -> CaptureGateDecision:
    mode = _env_mode()
    trace: dict[str, Any] = {
        "mode": mode,
        "aiEnabled": bool(ai_enabled),
        "domMedia": dom_media if isinstance(dom_media, dict) else None,
    }

    text = analyze_post_text_for_capture(search_keyword=search_keyword, post_body=dom_post_text)

    mv, mr = dom_media_hard_veto(mode=str(mode), dom_media=dom_media)
    if mv:
        trace["mediaVeto"] = mr
        return CaptureGateDecision(
            True,
            mode,
            text,
            _image_discards(classifier_result if ai_enabled else None),
            f"reject:{mr}",
            trace,
        )

    text = _enhance_text_with_optional_embedding(search_keyword, dom_post_text, text)

    img_bad = _image_discards(classifier_result if ai_enabled else None)
    trace["textReason"] = text.reason
    trace["textScore"] = text.score
    trace["textPass"] = text.pass_gate
    trace["imageDiscardModel"] = bool(img_bad)

    if mode == "text_only":
        d = not text.pass_gate
        trace["decisionPath"] = "text_only"
        return CaptureGateDecision(
            bool(d), mode, text, img_bad, "reject:text" if d else "keep:text", trace
        )

    if mode == "image_only":
        if not ai_enabled:
            trace["decisionPath"] = "image_only_no_classifier"
            return CaptureGateDecision(False, mode, None, False, "keep:no_classifier", trace)
        d = img_bad
        trace["decisionPath"] = "image_only"
        return CaptureGateDecision(
            bool(d), mode, None, img_bad, "reject:image" if d else "keep:image", trace
        )

    if mode == "both":
        img_veto = bool(ai_enabled and img_bad)
        txt_veto = not text.pass_gate
        d = img_veto or txt_veto
        trace["decisionPath"] = "both_and"
        trace["imageVeto"] = img_veto
        trace["textVeto"] = txt_veto
        parts = [p for p, x in [("image", img_veto), ("text", txt_veto)] if x]
        summ = f"reject:{'+'.join(parts)}" if parts else "keep:both"
        return CaptureGateDecision(bool(d), mode, text, img_bad, summ, trace)

    # weighted
    wi = _env_float("POST_CAPTURE_WEIGHT_IMAGE", 0.62)
    wt = _env_float("POST_CAPTURE_WEIGHT_TEXT", 0.38)
    stot = wi + wt
    if stot <= 1e-9:
        wi, wt = 0.62, 0.38
        stot = 1.0
    wi, wt = wi / stot, wt / stot
    thr = _env_float("POST_CAPTURE_WEIGHTED_KEEP_THRESHOLD", 0.52)
    ic = _image_keep_confidence(classifier_result if ai_enabled else None)
    tc = float(text.score)
    combined = wi * ic + wt * tc
    trace.update(
        {
            "decisionPath": "weighted",
            "weights": {"image": wi, "text": wt},
            "imageKeepConfidence": ic,
            "combined": combined,
            "threshold": thr,
        }
    )

    if not text.pass_gate and "sponsored_or_ad_marker" in (text.reason or ""):
        trace["hardVeto"] = "sponsored_text"
        return CaptureGateDecision(True, "weighted", text, img_bad, "reject:sponsored_text", trace)

    d = combined < thr
    return CaptureGateDecision(
        bool(d), "weighted", text, img_bad, "reject:weighted" if d else "keep:weighted", trace
    )


def attempt_vlm_rescue_after_reject(
    *,
    discarded: CaptureGateDecision,
    image_path: Path | None,
    search_keyword: str,
    dom_post_text: str,
) -> tuple[bool, dict[str, Any]]:
    trace: dict[str, Any] = {"ran": False}
    if not discarded.discard or image_path is None:
        trace["skipped"] = "not_needed"
        return discarded.discard, trace

    raw = os.getenv("POST_CAPTURE_VLM_RESCUE", "0") or ""
    if str(raw).strip().lower() not in {"1", "true", "yes", "on"}:
        trace["skipped"] = "disabled"
        return True, trace

    pv = str(os.getenv("POST_VLM_PROVIDER") or "").strip().lower()
    try:
        from app.worker.post_classifier.vlm_judge import judge_facebook_post_screenshot, provider_config_ready

        if pv not in {"openai", "gemini"} or not provider_config_ready(pv):
            trace["skipped"] = "provider_not_ready"
            return True, trace
    except Exception as e:
        trace["skipped"] = f"import:{e}"
        return True, trace

    tout = float(os.getenv("POST_VLM_TIMEOUT_SEC", "22") or "22")
    vr = judge_facebook_post_screenshot(
        image_path=Path(image_path),
        search_keyword=search_keyword,
        dom_snippet=str(dom_post_text or "")[:6000],
        provider=pv,
        timeout_sec=tout,
    )
    trace.update(
        {"ran": True, "vlmOk": vr.get("ok"), "accept": vr.get("accept"), "elapsedMs": vr.get("elapsed_ms")}
    )
    if vr.get("ok") and bool(vr.get("accept")):
        cf_min = float(os.getenv("POST_CAPTURE_VLM_RESCUE_MIN_CONF", "0.55") or "0.55")
        cf = vr.get("confidence")
        try:
            if cf is not None and float(cf) < cf_min:
                trace["skipped"] = "low_confidence"
                return True, trace
        except Exception:
            pass
        trace["rescued"] = True
        return False, trace

    trace["rescued"] = False
    return True, trace


def public_config_snapshot() -> dict[str, Any]:
    return {
        "mode": _env_mode(),
        "textMinCoverage": _env_float("POST_CAPTURE_TEXT_MIN_COVERAGE", 0.34),
        "rejectSponsoredText": os.getenv("POST_CAPTURE_REJECT_SPONSORED_TEXT", "1"),
        "weightedThreshold": _env_float("POST_CAPTURE_WEIGHTED_KEEP_THRESHOLD", 0.52),
        "weightImage": _env_float("POST_CAPTURE_WEIGHT_IMAGE", 0.62),
        "weightText": _env_float("POST_CAPTURE_WEIGHT_TEXT", 0.38),
        "textEmbed": os.getenv("POST_CAPTURE_TEXT_EMBED", "0"),
        "embedBlend": _env_float("POST_CAPTURE_EMBED_BLEND", 0.35),
        "vlmRescue": os.getenv("POST_CAPTURE_VLM_RESCUE", "0"),
        "media": public_media_config(),
    }
