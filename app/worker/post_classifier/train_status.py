from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

from app.utils.paths import repo_root
from app.utils.timeutil import utc_now_iso
from app.worker.post_classifier.features import SUPPORTED_EXTS


def status_file_path() -> Path:
    return repo_root() / "app" / "storage" / "post_classifier_train_status.json"


def write_train_status(payload: dict) -> None:
    p = status_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    out = dict(payload)
    out.setdefault("updatedAt", utc_now_iso())
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def read_train_status() -> dict | None:
    p = status_file_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def count_images_under(root: Path, cap: int = 10_000) -> tuple[int, bool]:
    if not root.exists():
        return 0, False
    n = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        n += 1
        if n >= cap:
            return cap, True
    return n, False


def model_file_path() -> Path:
    return repo_root() / "app" / "worker" / "post_classifier" / "model.json"


def build_public_status() -> dict:
    data_root = repo_root() / "post_classifier_data"
    neg_dir = Path(os.getenv("POST_CLASSIFIER_NEG_DIR", str(data_root / "negative")))
    # Cap scans so the status endpoint stays cheap when polling from the UI.
    _cap = 2500
    neg_live, neg_trunc = count_images_under(neg_dir, cap=_cap)

    mp = model_file_path()
    model_exists = mp.exists()
    model_mtime_iso: str | None = None
    model_created_at: float | None = None
    model_kind: str | None = None
    model_suggested_thr: float | None = None
    model_hashes: int | None = None
    model_hash_max_dist: int | None = None
    model_deep_k: int | None = None
    if model_exists:
        try:
            model_mtime_iso = _dt.datetime.fromtimestamp(
                mp.stat().st_mtime, tz=_dt.timezone.utc
            ).isoformat()
        except Exception:
            model_mtime_iso = None
        try:
            raw = json.loads(mp.read_text(encoding="utf-8"))
            model_created_at = float(raw.get("created_at") or 0.0) or None
            model_kind = str(raw.get("kind") or "").strip().lower() or None
            try:
                v = raw.get("suggested_reject_threshold")
                model_suggested_thr = float(v) if v is not None else None
            except Exception:
                model_suggested_thr = None
            try:
                hs = raw.get("negative_hashes_hex") or []
                model_hashes = int(len(hs)) if isinstance(hs, list) else None
            except Exception:
                model_hashes = None
            try:
                model_hash_max_dist = int(raw.get("max_hamming") or 0) or None
            except Exception:
                model_hash_max_dist = None
            try:
                cs = raw.get("centroids") or []
                model_deep_k = int(len(cs)) if isinstance(cs, list) else None
            except Exception:
                model_deep_k = None
        except Exception:
            model_created_at = None
            model_kind = None
            model_suggested_thr = None
            model_hashes = None
            model_hash_max_dist = None
            model_deep_k = None

    trained_created_iso: str | None = None
    if model_created_at and model_created_at > 1.0:
        try:
            trained_created_iso = _dt.datetime.fromtimestamp(
                model_created_at, tz=_dt.timezone.utc
            ).isoformat()
        except Exception:
            trained_created_iso = None

    last = read_train_status()
    raw_enabled = os.getenv("POST_CLASSIFIER_ENABLED", "")
    if str(raw_enabled or "").strip() == "":
        enabled_mode = "auto"
        enabled_forced = None
    else:
        enabled_mode = "forced"
        enabled_forced = (str(raw_enabled).strip().lower() in {"1", "true", "yes", "on"})
    # Effective enabled matches worker logic: auto-enable only for trained reject-only models (avoid legacy linear_v1).
    auto_enabled = bool(model_kind in {"deep_rejector_v1", "hybrid_rejector_v1", "rejector_v1"})
    enabled_effective = bool(enabled_forced) if enabled_forced is not None else bool(auto_enabled)
    # Runtime threshold: prefer explicit env; else use deep model suggested threshold if present; else default.
    thr_raw = os.getenv("POST_CLASSIFIER_REJECT_THRESHOLD", "").strip()
    thr_legacy = os.getenv("POST_CLASSIFIER_THRESHOLD", "").strip()
    if thr_raw != "":
        runtime_thr = float(thr_raw)
        thr_source = "env:POST_CLASSIFIER_REJECT_THRESHOLD"
    elif thr_legacy != "":
        runtime_thr = float(thr_legacy)
        thr_source = "env:POST_CLASSIFIER_THRESHOLD"
    elif model_suggested_thr is not None:
        runtime_thr = float(model_suggested_thr)
        thr_source = "model:suggested_reject_threshold"
    else:
        runtime_thr = 0.85
        thr_source = "default"

    try:
        from app.worker.post_capture_decision.decision import public_config_snapshot

        _gate = public_config_snapshot()
    except Exception:
        _gate = {}

    return {
        "runtime": {
            "postClassifierEnabled": enabled_effective,
            "postClassifierEnabledMode": enabled_mode,
            "postClassifierAutoEnabled": auto_enabled,
            # Reject-only: threshold is a "reject when similarity-to-negative >= threshold"
            "threshold": float(runtime_thr),
            "thresholdSource": str(thr_source),
            "budgetSec": float(os.getenv("POST_CLASSIFIER_BUDGET_SEC", "3.5") or "3.5"),
            "engine": (os.getenv("POST_CLASSIFIER_ENGINE", "deep") or "deep").strip().lower(),
            "onnxVariant": (os.getenv("POST_CLASSIFIER_ONNX_VARIANT", "fp32") or "fp32").strip().lower(),
            "deepRecheckMargin": float(os.getenv("POST_CLASSIFIER_DEEP_RECHECK_MARGIN", "0.03") or "0.03"),
            "deepEmbCache": int(os.getenv("POST_CLASSIFIER_DEEP_EMB_CACHE", "256") or "256"),
            "hashMaxDist": int(os.getenv("POST_CLASSIFIER_HASH_MAX_DIST", os.getenv("POST_CLASSIFIER_HASH_MAX_HAMMING", "6")) or "6"),
            "ortThreads": int(os.getenv("POST_CLASSIFIER_ORT_THREADS", "0") or "0"),
            "ortInterThreads": int(os.getenv("POST_CLASSIFIER_ORT_INTER_THREADS", "0") or "0"),
            "postCaptureDecision": _gate,
        },
        "dataset": {
            "root": str(data_root),
            "negativeDir": str(neg_dir),
            "negativeCount": neg_live,
            "countsTruncated": bool(neg_trunc),
        },
        "model": {
            "path": str(mp),
            "exists": model_exists,
            "fileModifiedAt": model_mtime_iso,
            "trainedCreatedAt": model_created_at,
            "trainedCreatedAtIso": trained_created_iso,
            "kind": model_kind,
            "suggestedRejectThreshold": model_suggested_thr,
            "deepK": model_deep_k,
            "hashes": model_hashes,
            "hashMaxDist": model_hash_max_dist,
        },
        "lastTrain": last,
    }
