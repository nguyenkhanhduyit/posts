from __future__ import annotations

from pathlib import Path
from io import BytesIO
from PIL import Image


def dhash64(path: Path, hash_size: int = 8) -> str:
    """
    Difference hash (dHash) for near-duplicate image detection.
    Returns a hex string (64-bit when hash_size=8).
    """
    img = Image.open(path).convert("L")
    # (hash_size+1) x hash_size
    img = img.resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
    pixels = list(img.getdata())
    rows = [pixels[i * (hash_size + 1) : (i + 1) * (hash_size + 1)] for i in range(hash_size)]
    bits = []
    for row in rows:
        for col in range(hash_size):
            bits.append(1 if row[col] > row[col + 1] else 0)
    # pack bits to int
    v = 0
    for b in bits:
        v = (v << 1) | b
    return f"{v:016x}"


def dhash64_int(path: Path, hash_size: int = 8) -> int:
    """
    dHash as a 64-bit integer for fast Hamming distance comparisons.
    """
    return int(dhash64(path, hash_size=hash_size), 16)

def dhash64_int_bytes(png_bytes: bytes, hash_size: int = 8) -> int:
    """
    dHash as a 64-bit integer from in-memory PNG bytes.
    Avoids disk I/O in tight screenshot loops.
    """
    img = Image.open(BytesIO(png_bytes)).convert("L")
    img = img.resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
    pixels = list(img.getdata())
    rows = [pixels[i * (hash_size + 1) : (i + 1) * (hash_size + 1)] for i in range(hash_size)]
    v = 0
    for row in rows:
        for col in range(hash_size):
            v = (v << 1) | (1 if row[col] > row[col + 1] else 0)
    return int(v)


def hamming_distance_u64(a: int, b: int) -> int:
    """
    Hamming distance between two 64-bit integers.
    """
    # Python 3.10+: int.bit_count()
    return (a ^ b).bit_count()

