from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image


SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass(frozen=True)
class ImageFeatures:
    # Keep this small & stable because it is used by the model.json
    aspect: float
    edge_density: float
    hv_edge_ratio: float
    textline_score: float
    transitions_score: float
    grayness: float
    mean_luma: float
    contrast: float
    entropy: float
    png_bytes_per_px: float

    def to_vector(self) -> list[float]:
        # Bias handled in model layer
        return [
            float(self.aspect),
            float(self.edge_density),
            float(self.hv_edge_ratio),
            float(self.textline_score),
            float(self.transitions_score),
            float(self.grayness),
            float(self.mean_luma),
            float(self.contrast),
            float(self.entropy),
            float(self.png_bytes_per_px),
        ]

    def to_dict(self) -> dict[str, float]:
        return {
            "aspect": float(self.aspect),
            "edge_density": float(self.edge_density),
            "hv_edge_ratio": float(self.hv_edge_ratio),
            "textline_score": float(self.textline_score),
            "transitions_score": float(self.transitions_score),
            "grayness": float(self.grayness),
            "mean_luma": float(self.mean_luma),
            "contrast": float(self.contrast),
            "entropy": float(self.entropy),
            "png_bytes_per_px": float(self.png_bytes_per_px),
        }


def iter_images(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in SUPPORTED_EXTS:
            yield p


def _shannon_entropy(hist: list[int], total: int) -> float:
    if total <= 0:
        return 0.0
    ent = 0.0
    inv = 1.0 / float(total)
    for c in hist:
        if c <= 0:
            continue
        p = float(c) * inv
        ent -= p * math.log(p + 1e-12, 2)
    # 8-bit entropy range: 0..8
    return float(ent)


def _grayness_score(img_rgb: Image.Image) -> float:
    """
    0..1: higher means closer to grayscale (screenshot/text tends to be low-color).
    Compute from mean absolute channel deviation.
    """
    img = img_rgb
    if img.mode != "RGB":
        img = img.convert("RGB")
    # Sample pixels (fast): RGB channel deviation averaged.
    px = list(img.getdata())
    n = 0
    acc = 0.0
    step = 1 if len(px) <= 50_000 else 3
    for r, g, b in px[::step]:
        m = (r + g + b) / 3.0
        acc += (abs(r - m) + abs(g - m) + abs(b - m)) / 3.0
        n += 1
    if n <= 0:
        return 0.0
    # Normalize by 255
    dev = acc / float(n) / 255.0
    # dev ~ 0 => grayscale; map to score
    return float(max(0.0, min(1.0, 1.0 - dev * 3.0)))


def extract_features(image_path: Path, max_side: int = 256) -> ImageFeatures:
    """
    Fast, CPU-only feature extraction. Designed to complete in milliseconds for typical screenshots.
    """
    with Image.open(image_path) as im0:
        w0, h0 = im0.size
        aspect = float(w0) / float(h0 or 1)

        # Work on a small normalized image for speed and consistent features.
        im_rgb = im0.convert("RGB")
        scale = float(max_side) / float(max(w0, h0) or 1)
        if scale < 1.0:
            w = max(32, int(w0 * scale))
            h = max(32, int(h0 * scale))
            im_rgb = im_rgb.resize((w, h), Image.BILINEAR)
        w, h = im_rgb.size

        grayness = _grayness_score(im_rgb)

        im_g = im_rgb.convert("L")
        # Histogram entropy (0..8)
        hist = list(im_g.histogram()[:256])
        entropy = _shannon_entropy(hist, w * h)

        # Edge density via cheap abs-diff gradients (no numpy).
        px = list(im_g.getdata())
        # Basic luminance stats (sampled) => mean + contrast.
        # Use a small sampling step to keep this cheap.
        sample_step = 1 if (w * h) <= 90_000 else 2
        sm = 0.0
        sm2 = 0.0
        sn = 0
        for v in px[::sample_step]:
            fv = float(v)
            sm += fv
            sm2 += fv * fv
            sn += 1
        mean_l = (sm / float(sn or 1)) / 255.0
        var_l = max(0.0, (sm2 / float(sn or 1)) - (sm / float(sn or 1)) ** 2)
        # Normalize contrast to 0..1-ish
        contrast = min(1.0, math.sqrt(var_l) / 80.0)
        # Adaptive threshold from mean absolute gradient.
        gx_sum = 0
        gy_sum = 0
        gcount = 0
        # sample stride to keep it fast on large downsamples
        stride = 1 if (w * h) <= 90_000 else 2
        for y in range(0, h - 1, stride):
            row = y * w
            for x in range(0, w - 1, stride):
                i = row + x
                a = px[i]
                gx_sum += abs(int(px[i + 1]) - int(a))
                gy_sum += abs(int(px[i + w]) - int(a))
                gcount += 2
        mean_g = float(gx_sum + gy_sum) / float(gcount or 1)
        thr = max(10.0, mean_g * 1.25)

        edges = 0
        edges_h = 0
        edges_v = 0
        samples = 0
        # second pass counting
        for y in range(0, h - 1, stride):
            row = y * w
            for x in range(0, w - 1, stride):
                i = row + x
                a = px[i]
                if abs(int(px[i + 1]) - int(a)) >= thr:
                    edges += 1
                    edges_h += 1
                if abs(int(px[i + w]) - int(a)) >= thr:
                    edges += 1
                    edges_v += 1
                samples += 2
        edge_density = float(edges) / float(samples or 1)
        hv_edge_ratio = float(edges_h + 1) / float(edges_v + 1)
        # squash ratio to 0..1
        hv_edge_ratio = float(hv_edge_ratio / (hv_edge_ratio + 1.0))

        # Text-line score: binarize + compute row projection "peakiness".
        # Screenshots of posts often have many horizontal text lines => strong row-sum variations.
        mean_l_pix = sum(px[:: max(1, stride)]) / float(len(px[:: max(1, stride)]) or 1)
        # Slightly bias to treat light background as 1 for "ink"
        cut = float(mean_l_pix) * 0.92
        row_sums = [0] * h
        for y in range(0, h, stride):
            row = y * w
            s = 0
            for x in range(0, w, stride):
                if px[row + x] <= cut:
                    s += 1
            row_sums[y] = s
        # Normalize and compute variance / mean
        vals = [v for v in row_sums[:: max(1, stride)]]
        if not vals:
            textline_score = 0.0
        else:
            m = sum(vals) / float(len(vals))
            var = sum((v - m) ** 2 for v in vals) / float(len(vals))
            # scale to ~0..1
            textline_score = float(max(0.0, min(1.0, (var / ((m + 1e-6) ** 2)) / 6.0)))

        # Transitions score: approximate "textiness" by counting black/white transitions
        # in a few sampled rows & columns. Text blocks produce many transitions.
        bin_stride = max(1, stride)
        bcut = cut
        trans = 0
        trans_n = 0
        # sample up to 24 rows
        row_step = max(1, h // 24)
        for y in range(0, h, row_step):
            row = y * w
            prev = 1 if px[row] <= bcut else 0
            for x in range(bin_stride, w, bin_stride):
                cur = 1 if px[row + x] <= bcut else 0
                if cur != prev:
                    trans += 1
                prev = cur
                trans_n += 1
        # sample up to 16 cols
        col_step = max(1, w // 16)
        for x in range(0, w, col_step):
            prev = 1 if px[x] <= bcut else 0
            for y in range(bin_stride, h, bin_stride):
                cur = 1 if px[y * w + x] <= bcut else 0
                if cur != prev:
                    trans += 1
                prev = cur
                trans_n += 1
        transitions_score = float(min(1.0, (float(trans) / float(trans_n or 1)) * 3.5))

        # PNG compressibility proxy: bytes per pixel on small image.
        buf = io.BytesIO()
        try:
            im_rgb.save(buf, format="PNG", optimize=True)
            png_bpp = float(len(buf.getvalue())) / float(w * h or 1)
        except Exception:
            png_bpp = 0.0

        return ImageFeatures(
            aspect=float(aspect),
            edge_density=float(edge_density),
            hv_edge_ratio=float(hv_edge_ratio),
            textline_score=float(textline_score),
            transitions_score=float(transitions_score),
            grayness=float(grayness),
            mean_luma=float(max(0.0, min(1.0, mean_l))),
            contrast=float(max(0.0, min(1.0, contrast))),
            entropy=float(entropy / 8.0),  # normalize 0..1
            png_bytes_per_px=float(min(1.0, png_bpp / 2.2)),  # normalize
        )

