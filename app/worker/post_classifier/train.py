from __future__ import annotations

import json
import math
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

from app.utils.paths import repo_root
from app.utils.timeutil import utc_now_iso
from app.worker.post_classifier.features import extract_features, iter_images
from app.utils.imagehash import dhash64_int
from app.worker.post_classifier.model import (
    CURRENT_FEATURE_NAMES,
    DeepBinaryModel,
    DeepRejectorModel,
    HybridRejectorModel,
    LinearModel,
    RejectorModel,
)
from app.worker.post_classifier.train_status import write_train_status


@dataclass(frozen=True)
class TrainConfig:
    lr: float = 0.35
    l2: float = 0.001
    epochs: int = 40
    seed: int = 1337
    max_per_class: int = 3000


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _shuffle(xs, ys, rnd: random.Random):
    idx = list(range(len(xs)))
    rnd.shuffle(idx)
    return [xs[i] for i in idx], [ys[i] for i in idx]


def _train_logreg(xs: list[list[float]], ys: list[int], cfg: TrainConfig) -> LinearModel:
    if not xs:
        return LinearModel.default()
    d = len(xs[0])
    w = [0.0] * d
    b = 0.0
    rnd = random.Random(cfg.seed)

    for ep in range(cfg.epochs):
        xs, ys = _shuffle(xs, ys, rnd)
        # SGD
        for x, y in zip(xs, ys):
            z = b + sum(wi * xi for wi, xi in zip(w, x))
            p = _sigmoid(z)
            # gradient of logloss
            g = (p - float(y))
            for i in range(d):
                w[i] -= cfg.lr * (g * x[i] + cfg.l2 * w[i])
            b -= cfg.lr * g

        # light lr decay
        if (ep + 1) % 10 == 0:
            cfg = TrainConfig(
                lr=max(0.05, cfg.lr * 0.7),
                l2=cfg.l2,
                epochs=cfg.epochs,
                seed=cfg.seed,
                max_per_class=cfg.max_per_class,
            )

    return LinearModel(
        feature_names=list(CURRENT_FEATURE_NAMES),
        weights=w,
        bias=b,
        created_at=time.time(),
    )


def main() -> int:
    data_root = repo_root() / "post_classifier_data"
    pos_dir = Path(os.getenv("POST_CLASSIFIER_POS_DIR", str(data_root / "positive")))
    neg_dir = Path(os.getenv("POST_CLASSIFIER_NEG_DIR", str(data_root / "negative")))
    out_model = repo_root() / "app" / "worker" / "post_classifier" / "model.json"
    run_started = time.perf_counter()
    try:
        pos_dir.mkdir(parents=True, exist_ok=True)
        neg_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    cfg = TrainConfig(
        lr=float(os.getenv("POST_CLASSIFIER_LR", "0.35").strip() or "0.35"),
        l2=float(os.getenv("POST_CLASSIFIER_L2", "0.001").strip() or "0.001"),
        epochs=int(os.getenv("POST_CLASSIFIER_EPOCHS", "40").strip() or "40"),
        seed=int(os.getenv("POST_CLASSIFIER_SEED", "1337").strip() or "1337"),
        max_per_class=int(os.getenv("POST_CLASSIFIER_MAX_PER_CLASS", "3000").strip() or "3000"),
    )

    write_train_status(
        {
            "phase": "running",
            "startedAt": utc_now_iso(),
            "message": "Đang train post classifier…",
            "positiveDir": str(pos_dir),
            "negativeDir": str(neg_dir),
            "modelOut": str(out_model),
        }
    )

    try:
        pos = list(iter_images(pos_dir))
        neg = list(iter_images(neg_dir))
        print(f"[train] positive={len(pos)} from {pos_dir}")
        print(f"[train] negative={len(neg)} from {neg_dir}")
        mode = (os.getenv("POST_CLASSIFIER_TRAIN_MODE", "auto") or "auto").strip().lower()
        # auto: if we have both classes -> train binary; else -> train rejector
        if mode == "auto":
            mode = "binary" if (len(pos) >= 20 and len(neg) >= 20) else "rejector"

        if mode == "binary":
            if len(pos) < 20 or len(neg) < 20:
                write_train_status(
                    {
                        "phase": "skipped",
                        "finishedAt": utc_now_iso(),
                        "exitCode": 2,
                        "ok": False,
                        "message": "Chưa đủ ảnh để train binary: cần ≥20 positive và ≥20 negative.",
                        "positiveCount": len(pos),
                        "negativeCount": len(neg),
                        "positiveDir": str(pos_dir),
                        "negativeDir": str(neg_dir),
                        "durationMs": int((time.perf_counter() - run_started) * 1000),
                    }
                )
                return 2

            # Deep binary: train logistic regression on projected deep embeddings (fast + accurate enough).
            from app.worker.post_classifier.deep import MOBILENETV3_FP32_URL, extract_embedding, project_and_normalize
            from app.worker.post_classifier.foundation import DEFAULT_FOUNDATION_URL, extract_foundation_embedding

            rp_dim = int(os.getenv("POST_CLASSIFIER_BINARY_RP_DIM", "128") or "128")
            rp_seed = int(os.getenv("POST_CLASSIFIER_BINARY_RP_SEED", str(cfg.seed)) or str(cfg.seed))
            rp_dim = max(32, min(256, rp_dim))

            rnd = random.Random(cfg.seed)
            rnd.shuffle(pos)
            rnd.shuffle(neg)
            pos = pos[: cfg.max_per_class]
            neg = neg[: cfg.max_per_class]

            xs: list[list[float]] = []
            ys: list[int] = []
            t0 = time.perf_counter()
            bad = 0
            for p in pos:
                try:
                    v = extract_embedding(p)
                    pv = project_and_normalize(v, d_out=rp_dim, seed=rp_seed)
                    xs.append([float(x) for x in pv.tolist()])
                    ys.append(1)
                except Exception:
                    bad += 1
            for p in neg:
                try:
                    v = extract_embedding(p)
                    pv = project_and_normalize(v, d_out=rp_dim, seed=rp_seed)
                    xs.append([float(x) for x in pv.tolist()])
                    ys.append(0)
                except Exception:
                    bad += 1
            feat_ms = int((time.perf_counter() - t0) * 1000)
            if len(xs) < 40:
                raise RuntimeError("Not enough valid samples for deep binary training.")

            lin = _train_logreg(xs, ys, cfg)
            # Suggest threshold: keep conservative default (slightly >0.5 to avoid false accepts).
            suggested_thr = float(os.getenv("POST_CLASSIFIER_BINARY_SUGGESTED_THRESHOLD", "0.60") or "0.60")
            model: DeepBinaryModel = DeepBinaryModel(
                weights=lin.weights,
                bias=lin.bias,
                rp_dim=rp_dim,
                rp_seed=rp_seed,
                created_at=time.time(),
                onnx_url=os.getenv("POST_CLASSIFIER_ONNX_URL", MOBILENETV3_FP32_URL),
                suggested_pos_threshold=suggested_thr,
            )

            # Upgrade to "AI 2026": optional foundation binary head (trained on DINOv2 embeddings),
            # but only used at inference for hard cases so speed stays high.
            if (os.getenv("POST_CLASSIFIER_FOUNDATION_ENABLED", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}:
                max_n = int(os.getenv("POST_CLASSIFIER_FOUNDATION_MAX_SAMPLES", "260") or "260")
                max_n = max(40, min(1200, max_n))
                f_rp_dim = int(os.getenv("POST_CLASSIFIER_FOUNDATION_RP_DIM", "128") or "128")
                f_rp_dim = max(32, min(256, f_rp_dim))
                f_rp_seed = int(os.getenv("POST_CLASSIFIER_FOUNDATION_RP_SEED", str(cfg.seed)) or str(cfg.seed))

                # Keep bounded: sample from each class
                pos_f = list(pos[: max_n // 2])
                neg_f = list(neg[: max_n // 2])
                xsf: list[list[float]] = []
                ysf: list[int] = []
                bad_f = 0
                t1 = time.perf_counter()
                for p in pos_f:
                    try:
                        v = extract_foundation_embedding(p)
                        pv = project_and_normalize(v, d_out=f_rp_dim, seed=f_rp_seed)
                        xsf.append([float(x) for x in pv.tolist()])
                        ysf.append(1)
                    except Exception:
                        bad_f += 1
                for p in neg_f:
                    try:
                        v = extract_foundation_embedding(p)
                        pv = project_and_normalize(v, d_out=f_rp_dim, seed=f_rp_seed)
                        xsf.append([float(x) for x in pv.tolist()])
                        ysf.append(0)
                    except Exception:
                        bad_f += 1
                f_ms = int((time.perf_counter() - t1) * 1000)
                if len(xsf) >= 40:
                    lin_f = _train_logreg(xsf, ysf, cfg)
                    suggested_f_thr = float(os.getenv("POST_CLASSIFIER_FOUNDATION_BINARY_SUGGESTED_THRESHOLD", str(suggested_thr)) or str(suggested_thr))
                    model = DeepBinaryModel(
                        weights=model.weights,
                        bias=model.bias,
                        rp_dim=model.rp_dim,
                        rp_seed=model.rp_seed,
                        created_at=model.created_at,
                        onnx_url=model.onnx_url,
                        suggested_pos_threshold=model.suggested_pos_threshold,
                        foundation_onnx_url=os.getenv("POST_CLASSIFIER_FOUNDATION_ONNX_URL", DEFAULT_FOUNDATION_URL),
                        foundation_weights=lin_f.weights,
                        foundation_bias=lin_f.bias,
                        foundation_rp_dim=f_rp_dim,
                        foundation_rp_seed=f_rp_seed,
                        foundation_suggested_pos_threshold=suggested_f_thr,
                    )
                    try:
                        write_train_status(
                            {
                                "phase": "running",
                                "startedAt": utc_now_iso(),
                                "message": f"Đang train foundation binary… n={len(xsf)} bad={bad_f} ms={f_ms}",
                            }
                        )
                    except Exception:
                        pass
            model.save(out_model)
            write_train_status(
                {
                    "phase": "ok",
                    "finishedAt": utc_now_iso(),
                    "exitCode": 0,
                    "ok": True,
                    "message": f"Train deep binary (positive+negative) xong. Ghi model: {out_model.name}",
                    "positiveCount": len(pos),
                    "negativeCount": len(neg),
                    "samplesUsed": len(xs),
                    "badSamples": bad,
                    "featureExtractMs": feat_ms,
                    "durationMs": int((time.perf_counter() - run_started) * 1000),
                    "modelPath": str(out_model),
                    "modelCreatedAt": model.created_at,
                    "modelKind": "deep_binary_v1",
                    "binaryRpDim": rp_dim,
                    "suggestedPosThreshold": suggested_thr,
                    "foundationBinaryTrained": bool(getattr(model, "foundation_weights", None)),
                }
            )
            return 0

        # rejector mode (negative-only): always train rejector when we have enough negatives.
        if len(neg) >= 20:
            engine = (os.getenv("POST_CLASSIFIER_ENGINE", "deep") or "deep").strip().lower()
            rnd = random.Random(cfg.seed)
            rnd.shuffle(neg)
            neg = neg[: cfg.max_per_class]
            if engine == "deep":
                try:
                    import numpy as np

                    from app.worker.post_classifier.deep import (
                        MOBILENETV3_FP32_URL,
                        extract_embedding,
                        farthest_point_sample,
                        kmeans_cosine_centroids,
                        mean_std,
                        project_and_normalize,
                    )
                    from app.worker.post_classifier.foundation import DEFAULT_FOUNDATION_URL, extract_foundation_embedding

                    vecs = []
                    t0 = time.perf_counter()
                    bad = 0
                    for p in neg:
                        try:
                            vecs.append(extract_embedding(p))
                        except Exception:
                            bad += 1
                    feat_ms = int((time.perf_counter() - t0) * 1000)
                    if len(vecs) < 20:
                        raise RuntimeError("Not enough valid negative samples for deep embeddings.")
                    mean, std = mean_std(vecs)
                    k = int(os.getenv("POST_CLASSIFIER_DEEP_K", "64") or "64")
                    iters = int(os.getenv("POST_CLASSIFIER_DEEP_KMEANS_ITERS", "12") or "12")
                    k = max(4, min(256, k))
                    if len(vecs) < k:
                        k = max(4, min(32, len(vecs)))
                    centroids = kmeans_cosine_centroids(vecs, k=k, iters=iters, seed=cfg.seed)
                    # Upgrade recognition: store diverse negative samples in a compact projected space (kNN).
                    rp_dim = int(os.getenv("POST_CLASSIFIER_DEEP_RP_DIM", "128") or "128")
                    rp_seed = int(os.getenv("POST_CLASSIFIER_DEEP_RP_SEED", str(cfg.seed)) or str(cfg.seed))
                    rp_dim = max(32, min(256, rp_dim))
                    m = int(os.getenv("POST_CLASSIFIER_DEEP_SAMPLE_M", "256") or "256")
                    m = max(32, min(512, m))
                    samp = farthest_point_sample(vecs, m=min(m, len(vecs)), seed=cfg.seed)
                    rp_samples = [project_and_normalize(v, d_out=rp_dim, seed=rp_seed).astype(np.float32).tolist() for v in samp]
                    # Also store near-duplicate fingerprints for ultra-fast reject (hash match).
                    hashes_hex: list[str] = []
                    for p in neg:
                        try:
                            hashes_hex.append(f"{int(dhash64_int(p)):016x}")
                        except Exception:
                            pass
                    max_hamming = int(os.getenv("POST_CLASSIFIER_HASH_MAX_DIST", "6") or "6")
                    max_hamming = max(0, min(32, max_hamming))
                    # Suggest a conservative reject threshold (high = reject less).
                    # Score is similarity-to-negative in [0,1]. We pick a high percentile.
                    try:
                        c = np.asarray(centroids, dtype=np.float32)
                        x = np.stack([np.asarray(v, dtype=np.float32) for v in vecs], axis=0)  # (N,D)
                        sims = x @ c.T  # cosine in [-1,1]
                        mx = sims.max(axis=1)
                        s01 = np.clip((mx + 1.0) * 0.5, 0.0, 1.0)
                        suggested = float(np.quantile(s01, 0.985))
                        suggested = float(max(0.80, min(0.98, suggested)))
                    except Exception:
                        suggested = None
                    # Also compute suggested threshold from kNN samples if available (more sensitive).
                    try:
                        if rp_samples:
                            mat = np.asarray(rp_samples, dtype=np.float32)  # (M, rp_dim)
                            pv_all = np.stack(
                                [project_and_normalize(v, d_out=rp_dim, seed=rp_seed) for v in vecs],
                                axis=0,
                            ).astype(np.float32)  # (N, rp_dim)
                            sims2 = pv_all @ mat.T
                            mx2 = sims2.max(axis=1)
                            s01_2 = np.clip((mx2 + 1.0) * 0.5, 0.0, 1.0)
                            suggested2 = float(np.quantile(s01_2, 0.985))
                            suggested2 = float(max(0.80, min(0.98, suggested2)))
                            if suggested is None:
                                suggested = suggested2
                            else:
                                suggested = float(max(suggested, suggested2))
                    except Exception:
                        pass
                    model = DeepRejectorModel(
                        mean=mean,
                        std=std,
                        centroids=centroids,
                        rp_dim=rp_dim,
                        rp_seed=rp_seed,
                        rp_negative_samples=rp_samples,
                        negative_hashes_hex=hashes_hex[:5000],
                        max_hamming=max_hamming,
                        created_at=time.time(),
                        onnx_url=os.getenv("POST_CLASSIFIER_ONNX_URL", MOBILENETV3_FP32_URL),
                        suggested_reject_threshold=suggested,
                    )

                    # Optional: foundation embedding samples (only if enabled, to keep training fast).
                    if (os.getenv("POST_CLASSIFIER_FOUNDATION_ENABLED", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}:
                        max_n = int(os.getenv("POST_CLASSIFIER_FOUNDATION_MAX_SAMPLES", "300") or "300")
                        max_n = max(40, min(2000, max_n))
                        rp_dim_f = int(os.getenv("POST_CLASSIFIER_FOUNDATION_RP_DIM", "128") or "128")
                        rp_dim_f = max(32, min(256, rp_dim_f))
                        rp_seed_f = int(os.getenv("POST_CLASSIFIER_FOUNDATION_RP_SEED", str(cfg.seed)) or str(cfg.seed))
                        # Sample negatives to keep foundation training time bounded.
                        neg_f = list(neg[: max_n])
                        fvecs = []
                        bad_f = 0
                        t1 = time.perf_counter()
                        for p in neg_f:
                            try:
                                fvecs.append(extract_foundation_embedding(p))
                            except Exception:
                                bad_f += 1
                        # Diverse samples, then project.
                        samp_f = farthest_point_sample(fvecs, m=min(256, len(fvecs)), seed=cfg.seed)
                        rp_samples_f = [
                            project_and_normalize(v, d_out=rp_dim_f, seed=rp_seed_f).astype(np.float32).tolist()
                            for v in samp_f
                        ]
                        # Suggested threshold from foundation space.
                        suggested_f = None
                        try:
                            if rp_samples_f and fvecs:
                                mat = np.asarray(rp_samples_f, dtype=np.float32)
                                pv_all = np.stack(
                                    [project_and_normalize(v, d_out=rp_dim_f, seed=rp_seed_f) for v in fvecs],
                                    axis=0,
                                ).astype(np.float32)
                                mx = (pv_all @ mat.T).max(axis=1)
                                s01 = np.clip((mx + 1.0) * 0.5, 0.0, 1.0)
                                suggested_f = float(np.quantile(s01, 0.985))
                                suggested_f = float(max(0.80, min(0.98, suggested_f)))
                        except Exception:
                            suggested_f = None
                        model = DeepRejectorModel(
                            mean=model.mean,
                            std=model.std,
                            centroids=model.centroids,
                            rp_dim=model.rp_dim,
                            rp_seed=model.rp_seed,
                            rp_negative_samples=model.rp_negative_samples,
                            negative_hashes_hex=model.negative_hashes_hex,
                            max_hamming=model.max_hamming,
                            created_at=model.created_at,
                            onnx_url=model.onnx_url,
                            suggested_reject_threshold=model.suggested_reject_threshold,
                            foundation_onnx_url=os.getenv("POST_CLASSIFIER_FOUNDATION_ONNX_URL", DEFAULT_FOUNDATION_URL),
                            foundation_rp_dim=rp_dim_f,
                            foundation_rp_seed=rp_seed_f,
                            foundation_rp_negative_samples=rp_samples_f,
                            foundation_suggested_reject_threshold=suggested_f,
                        )
                        try:
                            write_train_status(
                                {
                                    "phase": "running",
                                    "startedAt": utc_now_iso(),
                                    "message": f"Đang build foundation samples… used={len(fvecs)} bad={bad_f} ms={int((time.perf_counter()-t1)*1000)}",
                                }
                            )
                        except Exception:
                            pass
                    model.save(out_model)
                    write_train_status(
                        {
                            "phase": "ok",
                            "finishedAt": utc_now_iso(),
                            "exitCode": 0,
                            "ok": True,
                            "message": f"Train deep rejector (CNN, negative-only) xong. Ghi model: {out_model.name}",
                            "negativeCount": len(neg),
                            "samplesUsed": len(vecs),
                            "badSamples": bad,
                            "featureExtractMs": feat_ms,
                            "durationMs": int((time.perf_counter() - run_started) * 1000),
                            "modelPath": str(out_model),
                            "modelCreatedAt": model.created_at,
                            "modelKind": "deep_rejector_v1",
                            "engine": "deep",
                            "deepK": k,
                            "deepKmeansIters": iters,
                            "deepRpDim": rp_dim,
                            "deepSampleM": len(rp_samples),
                            "suggestedRejectThreshold": suggested,
                            "foundationEnabled": (os.getenv("POST_CLASSIFIER_FOUNDATION_ENABLED", "0") or "0"),
                            "foundationRpDim": getattr(model, "foundation_rp_dim", None),
                            "foundationSampleM": len(getattr(model, "foundation_rp_negative_samples", []) or []),
                            "foundationSuggestedRejectThreshold": getattr(model, "foundation_suggested_reject_threshold", None),
                        }
                    )
                    return 0
                except Exception as e:
                    # fall back to hybrid below
                    write_train_status(
                        {
                            "phase": "warn",
                            "finishedAt": utc_now_iso(),
                            "exitCode": 0,
                            "ok": True,
                            "message": f"Deep engine failed, fallback to hybrid: {e}",
                            "negativeCount": len(neg),
                            "durationMs": int((time.perf_counter() - run_started) * 1000),
                            "engine": "hybrid",
                        }
                    )
            vecs2: list[list[float]] = []
            hashes_hex: list[str] = []
            t0 = time.perf_counter()
            bad = 0
            for p in neg:
                try:
                    f = extract_features(p)
                    vecs2.append(f.to_vector())
                    try:
                        hashes_hex.append(f"{int(dhash64_int(p)):016x}")
                    except Exception:
                        pass
                except Exception:
                    bad += 1
            feat_ms = int((time.perf_counter() - t0) * 1000)
            if len(vecs2) < 20:
                msg = "Không đủ mẫu negative hợp lệ sau khi extract features."
                write_train_status(
                    {
                        "phase": "skipped",
                        "finishedAt": utc_now_iso(),
                        "exitCode": 2,
                        "ok": False,
                        "message": msg,
                        "negativeCount": len(neg),
                        "badSamples": bad,
                        "featureExtractMs": feat_ms,
                        "durationMs": int((time.perf_counter() - run_started) * 1000),
                    }
                )
                return 2

            d = len(vecs2[0])
            mean = [0.0] * d
            for v in vecs2:
                for i in range(d):
                    mean[i] += float(v[i])
            mean = [m / float(len(vecs2) or 1) for m in mean]
            std = [0.0] * d
            for v in vecs2:
                for i in range(d):
                    dv = float(v[i]) - float(mean[i])
                    std[i] += dv * dv
            std = [math.sqrt(s / float(len(vecs2) or 1)) for s in std]
            std = [max(1e-6, float(s)) for s in std]

            max_hamming = int(os.getenv("POST_CLASSIFIER_HASH_MAX_DIST", "6") or "6")
            model = HybridRejectorModel(
                feature_names=list(CURRENT_FEATURE_NAMES),
                mean=mean,
                std=std,
                negative_hashes_hex=hashes_hex[:5000],
                max_hamming=max(0, min(32, max_hamming)),
                created_at=time.time(),
            )
            model.save(out_model)
            write_train_status(
                {
                    "phase": "ok",
                    "finishedAt": utc_now_iso(),
                    "exitCode": 0,
                    "ok": True,
                    "message": f"Train hybrid rejector (negative-only) xong. Ghi model: {out_model.name}",
                    "negativeCount": len(neg),
                    "samplesUsed": len(vecs2),
                    "badSamples": bad,
                    "featureExtractMs": feat_ms,
                    "durationMs": int((time.perf_counter() - run_started) * 1000),
                    "modelPath": str(out_model),
                    "modelCreatedAt": model.created_at,
                    "modelKind": "hybrid_rejector_v1",
                    "hashes": len(hashes_hex),
                    "hashMaxDist": model.max_hamming,
                }
            )
            return 0

        if len(neg) < 20:
            print("[train] Not enough negatives. Add >=20 images to post_classifier_data/negative/, then retry.")
            write_train_status(
                {
                    "phase": "skipped",
                    "finishedAt": utc_now_iso(),
                    "exitCode": 2,
                    "ok": False,
                    "message": "Chưa đủ ảnh negative: cần ≥20 ảnh trong post_classifier_data/negative/.",
                    "negativeCount": len(neg),
                    "negativeDir": str(neg_dir),
                    "durationMs": int((time.perf_counter() - run_started) * 1000),
                }
            )
            return 2

        # Legacy feature-based binary training disabled; deep binary is preferred now.
        write_train_status(
            {
                "phase": "skipped",
                "finishedAt": utc_now_iso(),
                "exitCode": 2,
                "ok": False,
                "message": "Legacy feature-based binary disabled. Use deep binary via POST_CLASSIFIER_TRAIN_MODE=binary.",
                "negativeCount": len(neg),
                "negativeDir": str(neg_dir),
                "durationMs": int((time.perf_counter() - run_started) * 1000),
            }
        )
        return 2

        rnd = random.Random(cfg.seed)
        rnd.shuffle(pos)
        rnd.shuffle(neg)
        pos = pos[: cfg.max_per_class]
        neg = neg[: cfg.max_per_class]

        xs: list[list[float]] = []
        ys: list[int] = []

        t0 = time.perf_counter()
        bad = 0
        for p in pos:
            try:
                f = extract_features(p)
                xs.append(f.to_vector())
                ys.append(1)
            except Exception:
                bad += 1
        for p in neg:
            try:
                f = extract_features(p)
                xs.append(f.to_vector())
                ys.append(0)
            except Exception:
                bad += 1

        feat_ms = int((time.perf_counter() - t0) * 1000)
        print(f"[train] feature extracted: n={len(xs)} bad={bad} elapsed_ms={feat_ms}")
        model = _train_logreg(xs, ys, cfg)
        model.save(out_model)
        print(f"[train] wrote model: {out_model}")
        print(json.dumps({"weights": model.weights, "bias": model.bias}, indent=2))
        write_train_status(
            {
                "phase": "ok",
                "finishedAt": utc_now_iso(),
                "exitCode": 0,
                "ok": True,
                "message": f"Train xong. Ghi model: {out_model.name}",
                "positiveCount": len(pos),
                "negativeCount": len(neg),
                "samplesUsed": len(xs),
                "badSamples": bad,
                "featureExtractMs": feat_ms,
                "durationMs": int((time.perf_counter() - run_started) * 1000),
                "modelPath": str(out_model),
                "modelCreatedAt": model.created_at,
            }
        )
        return 0
    except Exception as e:
        write_train_status(
            {
                "phase": "error",
                "finishedAt": utc_now_iso(),
                "exitCode": 1,
                "ok": False,
                "message": str(e),
                "durationMs": int((time.perf_counter() - run_started) * 1000),
                "negativeDir": str(neg_dir),
            }
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())

