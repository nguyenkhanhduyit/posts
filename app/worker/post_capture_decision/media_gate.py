"""DOM-derived media/permalinks — fast structural signals."""

from __future__ import annotations

import os


def dom_media_hard_veto(
    *,
    mode: str,
    dom_media: dict | None,
) -> tuple[bool, str]:
    """
    When POST_CAPTURE_REQUIRE_MEDIA=1 (and optionally POST_CAPTURE_MEDIA_OR_PERMALINK),
    veto captures that look like bare text/chrome with no substantive media.

    Applies to text-heavy modes (text_only, both, weighted). Skipped for image_only.
    """
    if mode not in {"text_only", "both", "weighted"}:
        return False, ""
    need = os.getenv("POST_CAPTURE_REQUIRE_MEDIA", "").strip().lower() in {"1", "true", "yes", "on"}
    if not need:
        return False, ""
    if not isinstance(dom_media, dict):
        return False, "no_media_struct"

    try:
        n_img = int(dom_media.get("imgVisibleCount") or 0)
        n_vid = int(dom_media.get("videoVisibleCount") or 0)
        perm = bool(dom_media.get("hasPermalink"))
    except Exception:
        return False, "bad_media_struct"

    or_perm = os.getenv("POST_CAPTURE_MEDIA_OR_PERMALINK", "1").strip().lower() in {"1", "true", "yes", "on"}
    if or_perm and perm:
        return False, ""

    if n_img > 0 or n_vid > 0:
        return False, ""

    return True, "require_media:no_img_no_video"


def public_media_config() -> dict[str, bool | str]:
    return {
        "requireDomMedia": os.getenv("POST_CAPTURE_REQUIRE_MEDIA", "0"),
        "orPermalink": os.getenv("POST_CAPTURE_MEDIA_OR_PERMALINK", "1"),
    }
