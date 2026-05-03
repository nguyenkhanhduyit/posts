from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import os

from app.utils.imagehash import dhash64_int, hamming_distance_u64
from app.worker.post_classifier.features import ImageFeatures, extract_features


CURRENT_FEATURE_NAMES = [
    "aspect",
    "edge_density",
    "hv_edge_ratio",
    "textline_score",
    "transitions_score",
    "grayness",
    "mean_luma",
    "contrast",
    "entropy",
    "png_bytes_per_px",
]


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return float(1.0 / (1.0 + ez))
    ez = math.exp(z)
    return float(ez / (1.0 + ez))


@dataclass(frozen=True)
class LinearModel:
    """
    Small logistic regression style model.
    score = sigmoid(bias + dot(weights, features))
    """

    weights: list[float]
    bias: float
    feature_names: list[str]
    created_at: float

    @staticmethod
    def default() -> "LinearModel":
        # Reasonable heuristics: prefer text-line score + grayness + moderate edge density.
        # Trained model will override.
        return LinearModel(
            feature_names=list(CURRENT_FEATURE_NAMES),
            weights=[
                -0.20,  # aspect
                2.10,  # edge_density
                0.80,  # hv_edge_ratio (screens/text tends to have more horizontal edges)
                3.80,  # textline_score
                2.10,  # transitions_score
                1.10,  # grayness
                0.35,  # mean_luma (posts often have white-ish background)
                0.75,  # contrast
                -0.55,  # entropy
                -0.85,  # png bpp
            ],
            bias=-1.25,
            created_at=time.time(),
        )

    @staticmethod
    def load(path: Path) -> "LinearModel":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LinearModel(
            weights=[float(x) for x in raw["weights"]],
            bias=float(raw["bias"]),
            feature_names=[str(x) for x in raw.get("feature_names") or []],
            created_at=float(raw.get("created_at") or 0.0),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "weights": self.weights,
                    "bias": self.bias,
                    "feature_names": self.feature_names,
                    "created_at": self.created_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _logit(self, feats: list[float]) -> float:
        s = float(self.bias)
        for w, x in zip(self.weights, feats):
            s += float(w) * float(x)
        return float(s)

    def score_from_features(self, f: ImageFeatures) -> float:
        vec = f.to_vector()
        # Backwards/forwards compatibility: if feature sets mismatch, align by names.
        if self.feature_names and len(self.weights) == len(self.feature_names) and self.feature_names != CURRENT_FEATURE_NAMES:
            cur_map = f.to_dict()
            z = float(self.bias)
            for name, w in zip(self.feature_names, self.weights):
                z += float(w) * float(cur_map.get(str(name), 0.0))
        else:
            # Fast path: zip over available values.
            z = self._logit(vec)
        return _sigmoid(z)


@dataclass(frozen=True)
class RejectorModel:
    """
    Negative-only model (reject-only).
    Trained ONLY on negative images. Inference returns similarity-to-negative (0..1).
    Reject only when similarity is high enough; otherwise keep.
    """

    feature_names: list[str]
    mean: list[float]
    std: list[float]
    created_at: float

    @staticmethod
    def load(path: Path) -> "RejectorModel":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return RejectorModel(
            feature_names=[str(x) for x in (raw.get("feature_names") or [])],
            mean=[float(x) for x in (raw.get("mean") or [])],
            std=[float(x) for x in (raw.get("std") or [])],
            created_at=float(raw.get("created_at") or 0.0),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "kind": "rejector_v1",
                    "feature_names": self.feature_names,
                    "mean": self.mean,
                    "std": self.std,
                    "created_at": self.created_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def neg_score(self, f: ImageFeatures) -> float:
        cur = f.to_dict()
        xs = [float(cur.get(n, 0.0)) for n in self.feature_names]
        if not xs or not self.mean or not self.std:
            return 0.0
        d = 0.0
        n = 0
        for x, m, s in zip(xs, self.mean, self.std):
            ss = float(s) if float(s) > 1e-6 else 1e-6
            d += abs((float(x) - float(m)) / ss)
            n += 1
        d = d / float(n or 1)
        return float(max(0.0, min(1.0, math.exp(-d))))


@dataclass(frozen=True)
class HybridRejectorModel:
    """
    Negative-only hybrid rejector (still fast, smarter than pure stats):
    - Statistical similarity on handcrafted features (rejector_v1)
    - Plus near-duplicate matching using dHash64 against known negative images
    """

    feature_names: list[str]
    mean: list[float]
    std: list[float]
    negative_hashes_hex: list[str]
    max_hamming: int
    created_at: float

    @staticmethod
    def load(path: Path) -> "HybridRejectorModel":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return HybridRejectorModel(
            feature_names=[str(x) for x in (raw.get("feature_names") or [])],
            mean=[float(x) for x in (raw.get("mean") or [])],
            std=[float(x) for x in (raw.get("std") or [])],
            negative_hashes_hex=[str(x) for x in (raw.get("negative_hashes_hex") or [])],
            max_hamming=int(raw.get("max_hamming") or 6),
            created_at=float(raw.get("created_at") or 0.0),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "kind": "hybrid_rejector_v1",
                    "feature_names": self.feature_names,
                    "mean": self.mean,
                    "std": self.std,
                    "negative_hashes_hex": self.negative_hashes_hex,
                    "max_hamming": int(self.max_hamming),
                    "created_at": self.created_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def neg_score(self, f: ImageFeatures) -> float:
        # reuse the same math as RejectorModel
        cur = f.to_dict()
        xs = [float(cur.get(n, 0.0)) for n in self.feature_names]
        if not xs or not self.mean or not self.std:
            return 0.0
        d = 0.0
        n = 0
        for x, m, s in zip(xs, self.mean, self.std):
            ss = float(s) if float(s) > 1e-6 else 1e-6
            d += abs((float(x) - float(m)) / ss)
            n += 1
        d = d / float(n or 1)
        return float(max(0.0, min(1.0, math.exp(-d))))

    def hash_match(self, image_path: Path) -> dict[str, Any]:
        try:
            q = int(dhash64_int(image_path))
        except Exception:
            return {"ok": False}
        best = None
        best_dist = 999
        for hx in self.negative_hashes_hex:
            try:
                h = int(str(hx).strip(), 16)
            except Exception:
                continue
            d = int(hamming_distance_u64(q, h))
            if d < best_dist:
                best_dist = d
                best = hx
                if best_dist <= 0:
                    break
        return {"ok": True, "bestDist": int(best_dist), "bestHash": best, "isMatch": bool(best_dist <= int(self.max_hamming))}


@dataclass(frozen=True)
class DeepRejectorModel:
    """
    CNN-based negative-only rejector (MobileNetV2 via ONNXRuntime CPU).
    Uses prototypes (k-means centroids) in embedding space for robust matching.
    Falls back to mean/std distance if centroids are absent (backwards compatible).
    """

    mean: list[float]
    std: list[float]
    centroids: list[list[float]] | None = None
    rp_dim: int | None = None
    rp_seed: int | None = None
    rp_negative_samples: list[list[float]] | None = None
    foundation_onnx_url: str | None = None
    foundation_rp_dim: int | None = None
    foundation_rp_seed: int | None = None
    foundation_rp_negative_samples: list[list[float]] | None = None
    foundation_suggested_reject_threshold: float | None = None
    negative_hashes_hex: list[str] | None = None
    max_hamming: int = 6
    created_at: float
    onnx_url: str | None = None
    suggested_reject_threshold: float | None = None

    @staticmethod
    def load(path: Path) -> "DeepRejectorModel":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return DeepRejectorModel(
            mean=[float(x) for x in (raw.get("mean") or [])],
            std=[float(x) for x in (raw.get("std") or [])],
            centroids=[list(map(float, c)) for c in (raw.get("centroids") or [])] or None,
            rp_dim=int(raw.get("rp_dim") or 0) or None,
            rp_seed=int(raw.get("rp_seed") or 0) or None,
            rp_negative_samples=[list(map(float, r)) for r in (raw.get("rp_negative_samples") or [])] or None,
            foundation_onnx_url=str(raw.get("foundation_onnx_url") or "") or None,
            foundation_rp_dim=int(raw.get("foundation_rp_dim") or 0) or None,
            foundation_rp_seed=int(raw.get("foundation_rp_seed") or 0) or None,
            foundation_rp_negative_samples=[
                list(map(float, r)) for r in (raw.get("foundation_rp_negative_samples") or [])
            ]
            or None,
            foundation_suggested_reject_threshold=(
                float(raw.get("foundation_suggested_reject_threshold"))
                if raw.get("foundation_suggested_reject_threshold") is not None
                else None
            ),
            negative_hashes_hex=[str(x) for x in (raw.get("negative_hashes_hex") or [])] or None,
            max_hamming=int(raw.get("max_hamming") or 6),
            created_at=float(raw.get("created_at") or 0.0),
            onnx_url=str(raw.get("onnx_url") or "") or None,
            suggested_reject_threshold=(
                float(raw.get("suggested_reject_threshold"))
                if raw.get("suggested_reject_threshold") is not None
                else None
            ),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "kind": "deep_rejector_v1",
                    "mean": self.mean,
                    "std": self.std,
                    "centroids": self.centroids or [],
                    "rp_dim": int(self.rp_dim or 0),
                    "rp_seed": int(self.rp_seed or 0),
                    "rp_negative_samples": self.rp_negative_samples or [],
                    "foundation_onnx_url": self.foundation_onnx_url,
                    "foundation_rp_dim": int(self.foundation_rp_dim or 0),
                    "foundation_rp_seed": int(self.foundation_rp_seed or 0),
                    "foundation_rp_negative_samples": self.foundation_rp_negative_samples or [],
                    "foundation_suggested_reject_threshold": self.foundation_suggested_reject_threshold,
                    "negative_hashes_hex": self.negative_hashes_hex or [],
                    "max_hamming": int(self.max_hamming),
                    "created_at": self.created_at,
                    "onnx_url": self.onnx_url,
                    "suggested_reject_threshold": self.suggested_reject_threshold,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def hash_match(self, image_path: Path) -> dict[str, Any]:
        if not self.negative_hashes_hex:
            return {"ok": False}
        try:
            q = int(dhash64_int(image_path))
        except Exception:
            return {"ok": False}
        best = None
        best_dist = 999
        for hx in self.negative_hashes_hex:
            try:
                h = int(str(hx).strip(), 16)
            except Exception:
                continue
            d = int(hamming_distance_u64(q, h))
            if d < best_dist:
                best_dist = d
                best = hx
                if best_dist <= 0:
                    break
        return {
            "ok": True,
            "bestDist": int(best_dist),
            "bestHash": best,
            "isMatch": bool(best_dist <= int(self.max_hamming)),
        }

    def neg_score_vec(self, vec: list[float]) -> float:
        if not vec:
            return 0.0
        scores: list[float] = []
        # kNN samples in random-projected space (captures fine-grain negatives).
        if self.rp_dim and self.rp_seed and self.rp_negative_samples:
            try:
                from app.worker.post_classifier.deep import max_cosine_similarity_mat, project_and_normalize

                pv = project_and_normalize(np.array(vec, dtype=np.float32), d_out=int(self.rp_dim), seed=int(self.rp_seed))
                mat = np.asarray(self.rp_negative_samples, dtype=np.float32)
                sim = float(max_cosine_similarity_mat(pv, mat))
                scores.append(max(0.0, min(1.0, (sim + 1.0) * 0.5)))
            except Exception:
                pass
        # Centroid matching with cosine similarity (coarse but fast).
        if self.centroids:
            try:
                from app.worker.post_classifier.deep import max_cosine_similarity

                sim = float(max_cosine_similarity(np.array(vec, dtype=np.float32), self.centroids))
                scores.append(max(0.0, min(1.0, (sim + 1.0) * 0.5)))
            except Exception:
                pass
        if scores:
            return float(max(scores))
        # Fallback: old mean/std heuristic
        if not self.mean or not self.std:
            return 0.0
        d = 0.0
        n = 0
        for x, m, s in zip(vec, self.mean, self.std):
            ss = float(s) if float(s) > 1e-6 else 1e-6
            d += abs((float(x) - float(m)) / ss)
            n += 1
        d = d / float(n or 1)
        return float(max(0.0, min(1.0, math.exp(-d))))

    def foundation_score_vec(self, vec: list[float]) -> float:
        """
        Foundation embedding similarity-to-negative (0..1), based on random-projected kNN samples.
        """
        if not vec or not self.foundation_rp_dim or not self.foundation_rp_seed or not self.foundation_rp_negative_samples:
            return 0.0
        try:
            from app.worker.post_classifier.deep import max_cosine_similarity_mat, project_and_normalize

            pv = project_and_normalize(
                np.array(vec, dtype=np.float32),
                d_out=int(self.foundation_rp_dim),
                seed=int(self.foundation_rp_seed),
            )
            mat = np.asarray(self.foundation_rp_negative_samples, dtype=np.float32)
            sim = float(max_cosine_similarity_mat(pv, mat))
            return float(max(0.0, min(1.0, (sim + 1.0) * 0.5)))
        except Exception:
            return 0.0


@dataclass(frozen=True)
class DeepBinaryModel:
    """
    Deep binary classifier (positive vs negative) in a compact projected embedding space.
    score = sigmoid(bias + dot(weights, projected_embedding))
    """

    weights: list[float]
    bias: float
    rp_dim: int
    rp_seed: int
    created_at: float
    onnx_url: str | None = None
    suggested_pos_threshold: float | None = None
    # Optional foundation cascade (ViT/DINOv2) for hard cases
    foundation_onnx_url: str | None = None
    foundation_weights: list[float] | None = None
    foundation_bias: float | None = None
    foundation_rp_dim: int | None = None
    foundation_rp_seed: int | None = None
    foundation_suggested_pos_threshold: float | None = None

    @staticmethod
    def load(path: Path) -> "DeepBinaryModel":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return DeepBinaryModel(
            weights=[float(x) for x in (raw.get("weights") or [])],
            bias=float(raw.get("bias") or 0.0),
            rp_dim=int(raw.get("rp_dim") or 128),
            rp_seed=int(raw.get("rp_seed") or 1337),
            created_at=float(raw.get("created_at") or 0.0),
            onnx_url=str(raw.get("onnx_url") or "") or None,
            suggested_pos_threshold=(
                float(raw.get("suggested_pos_threshold"))
                if raw.get("suggested_pos_threshold") is not None
                else None
            ),
            foundation_onnx_url=str(raw.get("foundation_onnx_url") or "") or None,
            foundation_weights=[float(x) for x in (raw.get("foundation_weights") or [])] or None,
            foundation_bias=(float(raw.get("foundation_bias")) if raw.get("foundation_bias") is not None else None),
            foundation_rp_dim=int(raw.get("foundation_rp_dim") or 0) or None,
            foundation_rp_seed=int(raw.get("foundation_rp_seed") or 0) or None,
            foundation_suggested_pos_threshold=(
                float(raw.get("foundation_suggested_pos_threshold"))
                if raw.get("foundation_suggested_pos_threshold") is not None
                else None
            ),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "kind": "deep_binary_v1",
                    "weights": self.weights,
                    "bias": self.bias,
                    "rp_dim": int(self.rp_dim),
                    "rp_seed": int(self.rp_seed),
                    "created_at": self.created_at,
                    "onnx_url": self.onnx_url,
                    "suggested_pos_threshold": self.suggested_pos_threshold,
                    "foundation_onnx_url": self.foundation_onnx_url,
                    "foundation_weights": self.foundation_weights or [],
                    "foundation_bias": self.foundation_bias,
                    "foundation_rp_dim": int(self.foundation_rp_dim or 0),
                    "foundation_rp_seed": int(self.foundation_rp_seed or 0),
                    "foundation_suggested_pos_threshold": self.foundation_suggested_pos_threshold,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def score_from_vec(self, vec: list[float]) -> float:
        if not vec or not self.weights:
            return 0.0
        z = float(self.bias)
        for w, x in zip(self.weights, vec):
            z += float(w) * float(x)
        return float(_sigmoid(z))

    def foundation_score_from_vec(self, vec: list[float]) -> float:
        if not vec or not self.foundation_weights:
            return 0.0
        z = float(self.foundation_bias or 0.0)
        for w, x in zip(self.foundation_weights, vec):
            z += float(w) * float(x)
        return float(_sigmoid(z))


_CACHED: dict[str, Any] = {"model": None, "path": None}


def load_model_cached(model_path: Path) -> LinearModel | RejectorModel | HybridRejectorModel:
    mp = str(model_path)
    if _CACHED.get("model") is not None and _CACHED.get("path") == mp:
        return _CACHED["model"]
    if model_path.exists():
        try:
            raw = json.loads(model_path.read_text(encoding="utf-8"))
            kind = str(raw.get("kind") or "").strip().lower()
            if kind == "deep_rejector_v1":
                m = DeepRejectorModel.load(model_path)
            elif kind == "deep_binary_v1":
                m = DeepBinaryModel.load(model_path)
            elif kind == "hybrid_rejector_v1":
                m = HybridRejectorModel.load(model_path)
            elif kind == "rejector_v1":
                m = RejectorModel.load(model_path)
            else:
                m = LinearModel.load(model_path)
        except Exception:
            m = LinearModel.default()
    else:
        m = LinearModel.default()
    _CACHED["model"] = m
    _CACHED["path"] = mp
    return m


def classify_image(
    image_path: Path,
    model_path: Path,
    threshold: float = 0.85,
    budget_seconds: float = 3.5,
) -> dict[str, Any]:
    """
    Returns dict: { ok, score, is_positive, features, elapsed_ms }
    If feature extraction exceeds budget, it returns ok=False and does not block the pipeline.
    """
    t0 = time.perf_counter()
    try:
        model = load_model_cached(model_path)
        if isinstance(model, DeepBinaryModel):
            from app.worker.post_classifier.deep import extract_embedding, project_and_normalize

            v = extract_embedding(image_path)
            elapsed = (time.perf_counter() - t0)
            if budget_seconds > 0 and elapsed > float(budget_seconds):
                return {
                    "ok": False,
                    "score": None,
                    "is_positive": None,
                    "features": None,
                    "elapsed_ms": int(elapsed * 1000),
                    "reason": "budget_exceeded",
                }
            pv = project_and_normalize(v, d_out=int(model.rp_dim), seed=int(model.rp_seed))
            score = float(model.score_from_vec([float(x) for x in pv.tolist()]))
            thr = float(threshold)
            if thr <= 0 and model.suggested_pos_threshold is not None:
                thr = float(model.suggested_pos_threshold)

            # Foundation cascade for hard cases (keeps speed: only runs near threshold and if enabled+trained).
            f_enabled = (os.getenv("POST_CLASSIFIER_FOUNDATION_ENABLED", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
            f_margin = float(os.getenv("POST_CLASSIFIER_FOUNDATION_BINARY_MARGIN", "0.06") or "0.06")
            f_min_budget = float(os.getenv("POST_CLASSIFIER_FOUNDATION_MIN_BUDGET_LEFT", "1.2") or "1.2")
            reasons = {"foundation": {"ran": False}}
            thr_eff = float(thr)
            score_eff = float(score)

            if (
                f_enabled
                and model.foundation_weights
                and model.foundation_rp_dim
                and model.foundation_rp_seed
                and (abs(score - thr_eff) <= f_margin)
            ):
                elapsed = (time.perf_counter() - t0)
                if (budget_seconds <= 0) or (float(budget_seconds) - elapsed >= f_min_budget):
                    try:
                        from app.worker.post_classifier.foundation import extract_foundation_embedding

                        fv = extract_foundation_embedding(image_path)
                        fpv = project_and_normalize(
                            fv,
                            d_out=int(model.foundation_rp_dim),
                            seed=int(model.foundation_rp_seed),
                        )
                        fscore = float(model.foundation_score_from_vec([float(x) for x in fpv.tolist()]))
                        fthr = float(thr_eff)
                        if model.foundation_suggested_pos_threshold is not None:
                            fthr = float(model.foundation_suggested_pos_threshold)
                        # Allow override
                        try:
                            fthr_env = os.getenv("POST_CLASSIFIER_FOUNDATION_POS_THRESHOLD", "").strip()
                            if fthr_env != "":
                                fthr = float(fthr_env)
                        except Exception:
                            pass
                        # Combine: if foundation is confident, trust it; otherwise keep base score.
                        if (fscore >= fthr + 0.10) or (fscore <= fthr - 0.10):
                            score_eff = float(fscore)
                            thr_eff = float(fthr)
                            reasons["foundation"] = {"ran": True, "score": fscore, "threshold": fthr, "used": True}
                        else:
                            reasons["foundation"] = {"ran": True, "score": fscore, "threshold": fthr, "used": False}
                    except Exception as e:
                        reasons["foundation"] = {"ran": False, "error": str(e)}

            keep = bool(score_eff >= float(thr_eff))
            return {
                "ok": True,
                "kind": "deep_binary_v1",
                "score": score_eff,
                "is_positive": keep,
                "features": None,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "threshold": float(thr_eff),
                "reasons": reasons,
            }
        if isinstance(model, DeepRejectorModel):
            from app.worker.post_classifier.deep import extract_embedding, extract_embedding_recheck

            thr = float(threshold)
            if thr <= 0 and model.suggested_reject_threshold is not None:
                thr = float(model.suggested_reject_threshold)

            # Fast path: near-duplicate reject via dHash.
            hm = model.hash_match(image_path)
            if hm.get("ok") and hm.get("isMatch"):
                return {
                    "ok": True,
                    "kind": "deep_rejector_v1",
                    "score": 1.0,
                    "is_positive": False,
                    "features": None,
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                    "threshold": thr,
                    "reasons": {"hashMatch": True, "hash": hm},
                }

            v = extract_embedding(image_path)
            elapsed = (time.perf_counter() - t0)
            if budget_seconds > 0 and elapsed > float(budget_seconds):
                return {
                    "ok": False,
                    "score": None,
                    "is_positive": None,
                    "features": None,
                    "elapsed_ms": int(elapsed * 1000),
                    "reason": "budget_exceeded",
                }

            ns1 = float(model.neg_score_vec([float(x) for x in v.tolist()]))

            # Open-set gating: dynamic threshold based on "image quality" heuristics (no positives needed).
            # Goal: reduce false rejects on hard/ambiguous images by requiring higher confidence to reject.
            gating_enabled = (os.getenv("POST_CLASSIFIER_OPENSET_GATING", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
            gate_margin = float(os.getenv("POST_CLASSIFIER_GATING_MARGIN", "0.06") or "0.06")
            gate_max_boost = float(os.getenv("POST_CLASSIFIER_GATING_MAX_BOOST", "0.10") or "0.10")
            gate_min_budget = float(os.getenv("POST_CLASSIFIER_GATING_MIN_BUDGET_LEFT", "0.15") or "0.15")
            thr_eff = float(thr)
            gate = {"enabled": gating_enabled, "applied": False}
            feats_for_gate = None
            if gating_enabled:
                # Only gate when score is near threshold (otherwise don't waste cycles).
                if abs(ns1 - thr) <= gate_margin:
                    elapsed = (time.perf_counter() - t0)
                    if (budget_seconds <= 0) or (float(budget_seconds) - elapsed >= gate_min_budget):
                        try:
                            feats_for_gate = extract_features(image_path)
                            f = feats_for_gate
                            # Build a crude "hardness/ambiguity" score in 0..1:
                            # - low edges/transitions/textlines => harder (more ambiguous)
                            # - very low entropy/contrast => flat/blurred overlays -> harder
                            hard = 0.0
                            hard += max(0.0, min(1.0, (0.08 - float(f.edge_density)) / 0.08)) * 0.35
                            hard += max(0.0, min(1.0, (0.35 - float(f.transitions_score)) / 0.35)) * 0.20
                            hard += max(0.0, min(1.0, (0.25 - float(f.textline_score)) / 0.25)) * 0.25
                            hard += max(0.0, min(1.0, (0.20 - float(f.contrast)) / 0.20)) * 0.10
                            hard += max(0.0, min(1.0, (3.5 - float(f.entropy)) / 3.5)) * 0.10
                            hard = float(max(0.0, min(1.0, hard)))
                            boost = float(max(0.0, min(gate_max_boost, hard * gate_max_boost)))
                            thr_eff = float(min(0.99, thr + boost))
                            gate = {
                                "enabled": True,
                                "applied": bool(boost > 1e-6),
                                "hard": hard,
                                "boost": boost,
                                "thrBase": float(thr),
                                "thrEff": float(thr_eff),
                            }
                        except Exception as _e:
                            gate = {"enabled": True, "applied": False, "error": str(_e)}

            # Comprehensive upgrade: near-threshold recheck with multi-crop embedding.
            # Reject-only safety: only reject if BOTH checks exceed threshold.
            margin = float(os.getenv("POST_CLASSIFIER_DEEP_RECHECK_MARGIN", "0.03") or "0.03")
            do_recheck = (margin > 0) and (abs(ns1 - thr) <= margin)
            ns2 = None
            if do_recheck:
                # Only recheck if we have time left in budget.
                elapsed = (time.perf_counter() - t0)
                if (budget_seconds <= 0) or (elapsed + 0.80 <= float(budget_seconds)):
                    try:
                        _, vavg = extract_embedding_recheck(image_path)
                        ns2 = float(model.neg_score_vec([float(x) for x in vavg.tolist()]))
                    except Exception:
                        ns2 = None

            if ns2 is None:
                reject = bool(ns1 >= thr_eff)
                ns = ns1
                reasons = {"rechecked": False, "hashMatch": False, "gate": gate}
            else:
                reject = bool((ns1 >= thr_eff) and (ns2 >= thr_eff))
                ns = float(max(ns1, ns2))  # for logging visibility only
                reasons = {"rechecked": True, "score1": ns1, "score2": ns2, "hashMatch": False, "gate": gate}

            # Foundation model cascade (AI 2026 style): only run on hard cases to preserve speed.
            foundation_enabled = (os.getenv("POST_CLASSIFIER_FOUNDATION_ENABLED", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
            foundation_margin = float(os.getenv("POST_CLASSIFIER_FOUNDATION_MARGIN", "0.04") or "0.04")
            foundation_min_budget = float(os.getenv("POST_CLASSIFIER_FOUNDATION_MIN_BUDGET_LEFT", "1.2") or "1.2")
            foundation_hard_min = float(os.getenv("POST_CLASSIFIER_FOUNDATION_HARD_MIN", "0.35") or "0.35")
            gate_hard = None
            try:
                if isinstance(gate, dict) and gate.get("hard") is not None:
                    gate_hard = float(gate.get("hard"))
            except Exception:
                gate_hard = None
            should_try_foundation = bool(reject) and (
                (gate_hard is None) or (gate_hard >= foundation_hard_min) or (abs(ns - thr_eff) <= foundation_margin)
            )
            if foundation_enabled and model.foundation_rp_negative_samples and should_try_foundation:
                elapsed = (time.perf_counter() - t0)
                if (budget_seconds <= 0) or (float(budget_seconds) - elapsed >= foundation_min_budget):
                    try:
                        from app.worker.post_classifier.foundation import extract_foundation_embedding

                        fv = extract_foundation_embedding(image_path)
                        fscore = float(model.foundation_score_vec([float(x) for x in fv.tolist()]))
                        fthr = float(
                            os.getenv(
                                "POST_CLASSIFIER_FOUNDATION_REJECT_THRESHOLD",
                                str(model.foundation_suggested_reject_threshold or "0.90"),
                            )
                            or (model.foundation_suggested_reject_threshold or 0.90)
                        )
                        reasons["foundation"] = {"score": fscore, "threshold": fthr, "ran": True}
                        # Safety: only reject if foundation also agrees.
                        if reject and (fscore < fthr):
                            reject = False
                            reasons["foundation"]["veto"] = True
                    except Exception as e:
                        reasons["foundation"] = {"ran": False, "error": str(e)}

            # Final comprehensive upgrade: "positive veto" (avoid false rejects).
            # Only runs when we are about to reject, and only if there is still budget time.
            if reject and (os.getenv("POST_CLASSIFIER_DEEP_POS_VETO", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}:
                elapsed = (time.perf_counter() - t0)
                if (budget_seconds <= 0) or (elapsed + 0.20 <= float(budget_seconds)):
                    try:
                        feats = feats_for_gate or extract_features(image_path)
                        pos_score = float(LinearModel.default().score_from_features(feats))
                        veto_thr = float(os.getenv("POST_CLASSIFIER_DEEP_VETO_POS_THRESHOLD", "0.80") or "0.80")
                        if pos_score >= veto_thr:
                            reject = False
                            reasons["positiveVeto"] = True
                            reasons["positiveScore"] = pos_score
                            reasons["vetoThreshold"] = veto_thr
                        else:
                            reasons["positiveVeto"] = False
                            reasons["positiveScore"] = pos_score
                            reasons["vetoThreshold"] = veto_thr
                    except Exception:
                        reasons["positiveVeto"] = "error"
            return {
                "ok": True,
                "kind": "deep_rejector_v1",
                "score": ns,
                "is_positive": (not reject),
                "features": None,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "threshold": thr_eff,
                "reasons": reasons,
            }
        feats = extract_features(image_path)
        elapsed = (time.perf_counter() - t0)
        if budget_seconds > 0 and elapsed > float(budget_seconds):
            return {
                "ok": False,
                "score": None,
                "is_positive": None,
                "features": None,
                "elapsed_ms": int(elapsed * 1000),
                "reason": "budget_exceeded",
            }
        if isinstance(model, HybridRejectorModel):
            ns = float(model.neg_score(feats))
            hm = model.hash_match(image_path)
            reject_by_hash = bool(hm.get("ok") and hm.get("isMatch"))
            reject_by_score = bool(ns >= float(threshold))
            reject = bool(reject_by_hash or reject_by_score)
            return {
                "ok": True,
                "kind": "hybrid_rejector_v1",
                "score": ns,
                "is_positive": (not reject),
                "features": feats.to_dict(),
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "hash": hm if hm.get("ok") else None,
                "reasons": {
                    "hashMatch": bool(reject_by_hash),
                    "scoreReject": bool(reject_by_score),
                },
            }
        if isinstance(model, RejectorModel):
            ns = float(model.neg_score(feats))
            reject = bool(ns >= float(threshold))
            return {
                "ok": True,
                "kind": "rejector_v1",
                "score": ns,  # similarity-to-negative
                "is_positive": (not reject),  # keep by default
                "features": feats.to_dict(),
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            }
        score = float(model.score_from_features(feats))
        return {
            "ok": True,
            "kind": "linear_v1",
            "score": float(score),
            "is_positive": bool(score >= float(threshold)),
            "features": feats.to_dict(),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "score": None,
            "is_positive": None,
            "features": None,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "reason": f"error:{e}",
        }

