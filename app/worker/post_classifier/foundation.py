from __future__ import annotations

import os
import urllib.request
from pathlib import Path
from typing import Any, Tuple

import numpy as np
from PIL import Image

from app.utils.paths import repo_root


# A lightweight foundation-style embedding model (ViT) as ONNX.
# Output is used as an embedding vector (CLS token or pooled output), L2-normalized.
DEFAULT_FOUNDATION_URL = "https://huggingface.co/sefaburak/dinov2-small-onnx/resolve/main/dinov2_vits14.onnx"


def _models_dir() -> Path:
    return repo_root() / "app" / "storage" / "models"


def ensure_foundation_onnx() -> Path:
    url = str(os.getenv("POST_CLASSIFIER_FOUNDATION_ONNX_URL", DEFAULT_FOUNDATION_URL)).strip() or DEFAULT_FOUNDATION_URL
    out = _models_dir() / "dinov2-vits14.onnx"
    if out.exists() and out.stat().st_size > 10_000_000:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with urllib.request.urlopen(url, timeout=90) as r:
        tmp.write_bytes(r.read())
    tmp.replace(out)
    return out


_CACHE: dict[str, Any] = {"sess": None, "path": None, "in": None, "out": None, "hw": None}
_EMB_CACHE: dict[str, Any] = {"by_key": {}, "order": []}


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v)) + 1e-12
    return (v.astype(np.float32) / n).astype(np.float32)


_MEAN_CHW = np.array([0.485, 0.456, 0.406], dtype=np.float32)[:, None, None]
_STD_CHW = np.array([0.229, 0.224, 0.225], dtype=np.float32)[:, None, None]


def _get_session() -> Tuple[Any, str, str, tuple[int, int] | None]:
    import onnxruntime as ort

    p = ensure_foundation_onnx()
    sp = str(p)
    if _CACHE.get("sess") is not None and _CACHE.get("path") == sp:
        return _CACHE["sess"], _CACHE["in"], _CACHE["out"], _CACHE["hw"]

    so = ort.SessionOptions()
    try:
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    except Exception:
        pass
    thr = int(os.getenv("POST_CLASSIFIER_FOUNDATION_ORT_THREADS", os.getenv("POST_CLASSIFIER_ORT_THREADS", "0")) or "0")
    if thr > 0:
        so.intra_op_num_threads = thr
    inter_thr = int(os.getenv("POST_CLASSIFIER_FOUNDATION_ORT_INTER_THREADS", os.getenv("POST_CLASSIFIER_ORT_INTER_THREADS", "0")) or "0")
    if inter_thr > 0:
        so.inter_op_num_threads = inter_thr

    sess = ort.InferenceSession(sp, sess_options=so, providers=["CPUExecutionProvider"])
    in0 = sess.get_inputs()[0]
    out0 = sess.get_outputs()[0]
    in_name = str(in0.name)
    out_name = str(out0.name)

    # Try to infer expected H,W from input shape (N,C,H,W).
    hw = None
    try:
        shp = list(in0.shape or [])
        if len(shp) >= 4:
            h = int(shp[-2]) if shp[-2] is not None else 0
            w = int(shp[-1]) if shp[-1] is not None else 0
            if h > 0 and w > 0:
                hw = (h, w)
    except Exception:
        hw = None

    _CACHE.update({"sess": sess, "path": sp, "in": in_name, "out": out_name, "hw": hw})
    return sess, in_name, out_name, hw


def _preprocess_nchw(im: Image.Image, hw: tuple[int, int] | None) -> np.ndarray:
    target = hw or (224, 224)
    im2 = im.convert("RGB").resize((int(target[1]), int(target[0])), Image.BILINEAR)
    x = (np.asarray(im2, dtype=np.float32) / 255.0).transpose(2, 0, 1)  # CHW
    x = (x - _MEAN_CHW) / _STD_CHW
    return x[None, :, :, :].astype(np.float32)


def _emb_cache_key(image_path: Path) -> str:
    try:
        st = image_path.stat()
        return f"{str(image_path)}|{int(st.st_size)}|{int(st.st_mtime_ns)}"
    except Exception:
        return str(image_path)


def _emb_cache_get(key: str) -> np.ndarray | None:
    try:
        v = _EMB_CACHE["by_key"].get(key)
        if v is None:
            return None
        return np.asarray(v, dtype=np.float32)
    except Exception:
        return None


def _emb_cache_put(key: str, vec: np.ndarray) -> None:
    try:
        max_items = int(os.getenv("POST_CLASSIFIER_FOUNDATION_EMB_CACHE", "64") or "64")
        if max_items <= 0:
            return
        by_key = _EMB_CACHE["by_key"]
        order = _EMB_CACHE["order"]
        if key in by_key:
            by_key[key] = vec.astype(np.float32)
            return
        by_key[key] = vec.astype(np.float32)
        order.append(key)
        while len(order) > max_items:
            k0 = order.pop(0)
            by_key.pop(k0, None)
    except Exception:
        return


def extract_foundation_embedding(image_path: Path) -> np.ndarray:
    """
    Returns a L2-normalized embedding vector.
    Handles common output shapes:
    - (1, D) pooled embedding
    - (1, T, D) token embeddings -> CLS token (t=0)
    """
    key = _emb_cache_key(image_path)
    cached = _emb_cache_get(key)
    if cached is not None and cached.size > 0:
        return _l2_normalize(cached)
    sess, in_name, out_name, hw = _get_session()
    with Image.open(image_path) as im0:
        x = _preprocess_nchw(im0, hw)
    y = sess.run([out_name], {in_name: x})[0]
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 3:
        # (N,T,D) -> CLS token
        v = y[0, 0, :].reshape(-1)
    elif y.ndim == 2:
        v = y[0, :].reshape(-1)
    else:
        v = y.reshape(-1)
    v = _l2_normalize(v)
    _emb_cache_put(key, v)
    return v

