from __future__ import annotations

import os
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from app.utils.paths import repo_root


DEFAULT_BACKBONE = "mobilenetv3"

# Lightweight CNN backbones for fast CPU embeddings (logits used as embedding, normalized).
# You can override with POST_CLASSIFIER_ONNX_URL.
MOBILENETV2_FP32_URL = "https://huggingface.co/onnxmodelzoo/mobilenetv2-7/resolve/main/mobilenetv2-7.onnx?download=true"
MOBILENETV2_INT8_URL = "https://huggingface.co/onnxmodelzoo/mobilenetv2-12-int8/resolve/main/mobilenetv2-12-int8.onnx?download=true"

# MobileNetV3 Large (newer, typically better features than V2 at similar cost).
MOBILENETV3_FP32_URL = "https://huggingface.co/Kalray/mobilenet-v3-large/resolve/main/mobilenetv3-large.onnx?download=true"
MOBILENETV3_INT8_URL = "https://huggingface.co/Kalray/mobilenet-v3-large/resolve/main/mobilenetv3-large-q.onnx?download=true"


def _models_dir() -> Path:
    return repo_root() / "app" / "storage" / "models"


def ensure_onnx_model() -> Path:
    backbone = (os.getenv("POST_CLASSIFIER_BACKBONE", DEFAULT_BACKBONE) or DEFAULT_BACKBONE).strip().lower()
    variant = (os.getenv("POST_CLASSIFIER_ONNX_VARIANT", "fp32") or "fp32").strip().lower()
    want_int8 = variant in {"int8", "quant", "quantized"}
    if backbone in {"mobilenetv3", "mbv3", "mobilenet-v3"}:
        default_url = MOBILENETV3_INT8_URL if want_int8 else MOBILENETV3_FP32_URL
        out_name = "mobilenetv3-int8.onnx" if want_int8 else "mobilenetv3-fp32.onnx"
    else:
        default_url = MOBILENETV2_INT8_URL if want_int8 else MOBILENETV2_FP32_URL
        out_name = "mobilenetv2-int8.onnx" if want_int8 else "mobilenetv2-fp32.onnx"
    url = str(os.getenv("POST_CLASSIFIER_ONNX_URL", default_url)).strip() or default_url
    out = _models_dir() / out_name
    if out.exists() and out.stat().st_size > 1_000_000:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with urllib.request.urlopen(url, timeout=60) as r:
        tmp.write_bytes(r.read())
    tmp.replace(out)
    return out


_CACHE: dict[str, Any] = {"sess": None, "path": None, "in": None, "out": None}


def _get_session():
    import onnxruntime as ort

    p = ensure_onnx_model()
    sp = str(p)
    if _CACHE.get("sess") is not None and _CACHE.get("path") == sp:
        return _CACHE["sess"], _CACHE["in"], _CACHE["out"]
    so = ort.SessionOptions()
    # Speed: let ORT optimize graph for CPU (safe for inference).
    try:
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    except Exception:
        pass
    try:
        so.execution_mode = ort.ExecutionMode.ORT_PARALLEL
    except Exception:
        pass
    # let ORT decide threads unless user sets it
    thr = int(os.getenv("POST_CLASSIFIER_ORT_THREADS", "0") or "0")
    if thr > 0:
        so.intra_op_num_threads = thr
    inter_thr = int(os.getenv("POST_CLASSIFIER_ORT_INTER_THREADS", "0") or "0")
    if inter_thr > 0:
        so.inter_op_num_threads = inter_thr
    sess = ort.InferenceSession(sp, sess_options=so, providers=["CPUExecutionProvider"])
    in_name = sess.get_inputs()[0].name
    out_name = sess.get_outputs()[0].name
    _CACHE["sess"] = sess
    _CACHE["path"] = sp
    _CACHE["in"] = in_name
    _CACHE["out"] = out_name
    return sess, in_name, out_name


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v)) + 1e-12
    return (v.astype(np.float32) / n).astype(np.float32)


_MEAN_CHW = np.array([0.485, 0.456, 0.406], dtype=np.float32)[:, None, None]
_STD_CHW = np.array([0.229, 0.224, 0.225], dtype=np.float32)[:, None, None]

_EMB_CACHE: dict[str, Any] = {"by_key": {}, "order": []}


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
        max_items = int(os.getenv("POST_CLASSIFIER_DEEP_EMB_CACHE", "256") or "256")
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


def _preprocess_nchw(im: Image.Image) -> np.ndarray:
    x = (np.asarray(im, dtype=np.float32) / 255.0).transpose(2, 0, 1)  # CHW
    x = (x - _MEAN_CHW) / _STD_CHW
    return x[None, :, :, :]  # NCHW


def _run_batch(x_nchw: np.ndarray) -> np.ndarray:
    sess, in_name, out_name = _get_session()
    y = sess.run([out_name], {in_name: x_nchw})[0]
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 1:
        return y.reshape(1, -1).astype(np.float32)
    if y.ndim == 2:
        return y.astype(np.float32)
    return y.reshape(y.shape[0], -1).astype(np.float32)


def extract_embedding(image_path: Path) -> np.ndarray:
    """
    Smart embedding from MobileNetV2 logits (1000-d), normalized.
    """
    key = _emb_cache_key(image_path)
    cached = _emb_cache_get(key)
    if cached is not None and cached.size > 0:
        return _l2_normalize(cached)
    with Image.open(image_path) as im0:
        im = im0.convert("RGB").resize((224, 224), Image.BILINEAR)
    x = _preprocess_nchw(im)
    y = _run_batch(x)  # (1,D)
    v = _l2_normalize(y.reshape(-1).astype(np.float32))
    _emb_cache_put(key, v)
    return v


def extract_embedding_recheck(image_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """
    Return (single_embedding, multi_crop_embedding_avg).
    Multi-crop is only 3 crops to keep CPU fast.
    """
    with Image.open(image_path) as im0:
        im_rgb = im0.convert("RGB")

    # Fast single: direct resize
    im_single = im_rgb.resize((224, 224), Image.BILINEAR)

    # Multi-crop: resize shorter side to 256 then take 3 crops (center + 2 corners).
    w, h = im_rgb.size
    if w <= 0 or h <= 0:
        raise ValueError("Invalid image size")
    scale = 256.0 / float(min(w, h))
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    im_big = im_rgb.resize((nw, nh), Image.BILINEAR)

    def _crop(x0: int, y0: int) -> Image.Image:
        x0 = max(0, min(int(x0), max(0, nw - 224)))
        y0 = max(0, min(int(y0), max(0, nh - 224)))
        return im_big.crop((x0, y0, x0 + 224, y0 + 224))

    cx = (nw - 224) // 2
    cy = (nh - 224) // 2
    crops = [
        _crop(cx, cy),  # center
        _crop(0, 0),  # top-left
        _crop(max(0, nw - 224), max(0, nh - 224)),  # bottom-right
    ]

    xs = [_preprocess_nchw(im_single)]
    for c in crops:
        xs.append(_preprocess_nchw(c))
    x_batch = np.concatenate(xs, axis=0).astype(np.float32)  # (N,3,224,224)
    yb = _run_batch(x_batch)  # (N,D)
    v1 = _l2_normalize(yb[0].reshape(-1).astype(np.float32))
    vs = np.stack([_l2_normalize(yb[i].reshape(-1).astype(np.float32)) for i in range(1, yb.shape[0])], axis=0)
    v_avg = _l2_normalize(np.mean(vs, axis=0))
    return v1, v_avg


def mean_std(vectors: list[np.ndarray]) -> tuple[list[float], list[float]]:
    arr = np.stack(vectors, axis=0)
    m = arr.mean(axis=0)
    s = arr.std(axis=0)
    s = np.maximum(s, 1e-6)
    return m.astype(np.float32).tolist(), s.astype(np.float32).tolist()


_RP_CACHE: dict[tuple[int, int, int], np.ndarray] = {}


def random_projection_matrix(d_in: int, d_out: int, seed: int) -> np.ndarray:
    """
    Deterministic random projection (Johnson–Lindenstrauss style).
    """
    d_in = int(max(1, d_in))
    d_out = int(max(1, min(512, d_out)))
    seed = int(seed)
    key = (d_in, d_out, seed)
    if key in _RP_CACHE:
        return _RP_CACHE[key]
    rng = np.random.default_rng(seed)
    w = rng.standard_normal((d_in, d_out), dtype=np.float32) / np.sqrt(float(d_out))
    _RP_CACHE[key] = w.astype(np.float32)
    return _RP_CACHE[key]


def project_and_normalize(v: np.ndarray, d_out: int = 128, seed: int = 1337) -> np.ndarray:
    x = np.asarray(v, dtype=np.float32).reshape(-1)
    w = random_projection_matrix(x.shape[0], int(d_out), int(seed))
    y = x @ w  # (d_out,)
    return _l2_normalize(np.asarray(y, dtype=np.float32))


def max_cosine_similarity_mat(vec: np.ndarray, mat: np.ndarray) -> float:
    """
    vec: (D,), mat: (N,D) both float32; assumes already normalized.
    """
    if vec is None:
        return 0.0
    v = _l2_normalize(np.asarray(vec, dtype=np.float32).reshape(-1))
    m = np.asarray(mat, dtype=np.float32)
    if m.ndim != 2 or m.shape[1] != v.shape[0] or m.shape[0] <= 0:
        return 0.0
    sims = m @ v  # (N,)
    return float(np.max(sims))


def farthest_point_sample(vectors: list[np.ndarray], m: int = 256, seed: int = 1337) -> list[np.ndarray]:
    """
    Pick representative samples (diverse negatives) for kNN scoring.
    vectors are assumed normalized. Uses greedy farthest-point sampling in cosine space.
    """
    if not vectors:
        return []
    xs = np.stack([_l2_normalize(v) for v in vectors], axis=0).astype(np.float32)  # (N,D)
    n = xs.shape[0]
    m = int(max(1, min(int(m), int(n))))
    rng = np.random.default_rng(int(seed))
    start = int(rng.integers(0, n))
    chosen = [start]
    # dist = 1 - max_cosine_to_chosen
    sims = xs @ xs[start].reshape(-1, 1)  # (N,1)
    best = sims.reshape(-1)
    for _ in range(1, m):
        # choose smallest best similarity (farthest)
        idx = int(np.argmin(best))
        chosen.append(idx)
        s2 = xs @ xs[idx].reshape(-1, 1)
        best = np.maximum(best, s2.reshape(-1))
    return [xs[i] for i in chosen]


def kmeans_cosine_centroids(
    vectors: list[np.ndarray],
    k: int = 64,
    iters: int = 12,
    seed: int = 1337,
) -> list[list[float]]:
    """
    Build K centroids for negative-only matching (fast + robust).
    Uses cosine similarity (vectors are L2-normalized).
    Returns centroids as list[list[float]] (each centroid normalized).
    """
    if not vectors:
        return []
    x = np.stack([_l2_normalize(v) for v in vectors], axis=0).astype(np.float32)  # (N,D)
    n, d = x.shape
    k = int(max(1, min(int(k), int(n), 256)))
    iters = int(max(1, min(int(iters), 50)))

    rng = np.random.default_rng(int(seed))
    # Init centroids from random samples (fast, good enough for our use).
    init_idx = rng.choice(n, size=k, replace=False) if n >= k else np.arange(n)
    c = x[init_idx].copy()  # (k,D)
    # Ensure normalized
    c = c / (np.linalg.norm(c, axis=1, keepdims=True) + 1e-12)

    for _ in range(iters):
        sims = x @ c.T  # (N,K), cosine similarity
        labels = np.argmax(sims, axis=1)  # (N,)
        # Update centroids
        new_c = np.zeros((k, d), dtype=np.float32)
        counts = np.zeros((k,), dtype=np.int32)
        for i in range(n):
            li = int(labels[i])
            new_c[li] += x[i]
            counts[li] += 1
        # Handle empty clusters by re-seeding from random points
        for j in range(k):
            if counts[j] <= 0:
                new_c[j] = x[int(rng.integers(0, n))]
                counts[j] = 1
        new_c = new_c / (np.linalg.norm(new_c, axis=1, keepdims=True) + 1e-12)
        c = new_c

    return c.astype(np.float32).tolist()


def max_cosine_similarity(vec: np.ndarray, centroids: list[list[float]]) -> float:
    if vec is None or not centroids:
        return 0.0
    v = _l2_normalize(np.asarray(vec, dtype=np.float32).reshape(-1))
    c = np.asarray(centroids, dtype=np.float32)
    if c.ndim != 2 or c.shape[1] != v.shape[0]:
        return 0.0
    sims = c @ v  # (K,)
    return float(np.max(sims))

