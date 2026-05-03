"""
Vision-language cloud APIs are intentionally not wired in this project.

All capture decisions rely on DOM heuristics + local ONNX (`post_classifier`).

Keeps stale imports/builds working with stable function names returning "disabled".
"""

from __future__ import annotations

import time
from pathlib import Path

from app.worker.ai_inference_policy import REMOTE_CLOUD_AI_ENABLED


def provider_config_ready(provider: str) -> bool:
    _ = provider
    return False


def vlm_capabilities_snapshot() -> dict[str, Any]:
    return {
        "externalCloudAiEnabled": bool(REMOTE_CLOUD_AI_ENABLED),
        "policy": "local_inference_only",
        "vlmUnavailable": True,
        "embeddingApiUnavailable": True,
    }


def judge_facebook_post_screenshot(
    *,
    image_path: Path,
    search_keyword: str,
    dom_snippet: str,
    provider: str,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    _ = (image_path, search_keyword, dom_snippet, provider, timeout_sec)
    return {
        "ok": False,
        "accept": None,
        "confidence": None,
        "category": None,
        "reason": None,
        "elapsed_ms": 0,
        "provider": str(provider or "").strip().lower(),
        "error": "vlm_cloud_disabled_project_policy_local_only",
        "cascadePasses": [],
        "tierConfig": "none",
        "tier": "",
        "model": "",
        "pass": "",
    }
