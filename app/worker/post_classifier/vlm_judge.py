"""Single-shot vision-language judgement (OpenAI / Gemini HTTPS). Optional rescue path for capture gates."""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name, "") or "").strip()
    if raw == "":
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _parse_model_json(raw_text: str) -> dict[str, Any] | None:
    if not raw_text:
        return None
    try:
        return json.loads(raw_text.strip())
    except Exception:
        pass
    m = _JSON_OBJECT_RE.search(raw_text.strip())
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _image_png_b64(image_path: Path, max_edge: int) -> tuple[str, str]:
    max_edge_i = max(480, min(int(max_edge), 4096))
    raw = Path(image_path).read_bytes()
    mime = "image/png"
    try:
        from io import BytesIO

        from PIL import Image as PILImage

        im = PILImage.open(BytesIO(raw))
        im = im.convert("RGBA") if im.mode not in {"RGB", "L"} else im.convert("RGB")
        w, h = im.size
        longest = max(w, h)
        if longest > max_edge_i:
            scale = float(max_edge_i) / float(longest)
            im = im.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), PILImage.Resampling.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="PNG", optimize=True)
        b = buf.getvalue()
    except Exception:
        b = raw
    return mime, base64.standard_b64encode(b).decode("ascii")


_SYSTEM = """You judge one Facebook screenshot (search results / feed). Decide if it shows ONE real content post worth keeping (not pure ad chrome, skeleton, checkpoint, sidebar-only).

Return ONLY JSON: {"accept":true/false,"confidence":0-1,"reason":"short Vietnamese"}."""


def provider_config_ready(provider: str) -> bool:
    p = str(provider or "").strip().lower()
    if p == "openai":
        return bool((os.getenv("OPENAI_API_KEY") or "").strip())
    if p == "gemini":
        return bool((os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip())
    return False


def judge_facebook_post_screenshot(
    *,
    image_path: Path,
    search_keyword: str,
    dom_snippet: str,
    provider: str,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    p = str(provider or "").strip().lower()
    t0 = time.perf_counter()
    fail = {
        "ok": False,
        "accept": None,
        "confidence": None,
        "reason": None,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "provider": p,
        "error": None,
    }
    tout = max(3.0, min(float(timeout_sec if timeout_sec is not None else _env_float("POST_VLM_TIMEOUT_SEC", 22)), 120))
    max_edge = int(_env_float("POST_VLM_MAX_IMG_EDGE", 1280))

    kw = str(search_keyword or "").strip()
    sn = str(dom_snippet or "").strip()[:6000]
    prompt = f"Keyword: «{kw}». DOM excerpt:\n{sn}\nScreenshot attached — keep?"
    mime, b64 = _image_png_b64(Path(image_path), max_edge=max_edge)

    try:
        if p == "openai":
            out = _openai_json(prompt, mime, b64, tout)
        elif p == "gemini":
            out = _gemini_json(prompt, mime, b64, tout)
        else:
            fail["error"] = f"bad_provider:{p}"
            fail["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
            return fail
    except Exception as e:
        fail["error"] = f"{type(e).__name__}:{e}"
        fail["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return fail

    data = _parse_model_json(str(out.get("text") or ""))
    if not isinstance(data, dict):
        fail["error"] = "bad_json"
        fail["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return fail
    try:
        acc = bool(data.get("accept"))
    except Exception:
        fail["error"] = "no_accept"
        fail["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return fail
    conf = None
    try:
        if data.get("confidence") is not None:
            conf = _clamp01(float(data.get("confidence")))
    except Exception:
        conf = None
    rs = str(data.get("reason") or "").strip()[:280] or None
    return {
        "ok": True,
        "accept": acc,
        "confidence": conf,
        "reason": rs,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "provider": p,
        "error": None,
    }


def _https_json(url: str, hdr: dict[str, str], body: dict, tout: float) -> tuple[int, dict | None, str]:
    b = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=b, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in hdr.items():
        req.add_header(k, v)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=tout, context=ctx) as r:
            raw = r.read().decode("utf-8", errors="replace")
            code = int(getattr(r, "status", 200) or 200)
            try:
                jd = json.loads(raw)
            except Exception:
                jd = None
            return code, jd if isinstance(jd, dict) else None, raw
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = (e.read() or b"").decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            jd = json.loads(raw)
        except Exception:
            jd = None
        return int(getattr(e, "code", 0)), jd if isinstance(jd, dict) else None, raw


def _openai_json(user: str, mime: str, b64: str, tout: float) -> dict[str, Any]:
    k = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not k:
        raise RuntimeError("no_OPENAI_API_KEY")
    md = str(os.getenv("POST_VLM_OPENAI_MODEL") or "gpt-4o-mini").strip()
    url = str(os.getenv("POST_VLM_OPENAI_URL") or "https://api.openai.com/v1/chat/completions").strip()
    payload = {
        "model": md,
        "temperature": 0.06,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "low"}},
                ],
            },
        ],
    }
    code, jd, raw = _https_json(url, {"Authorization": f"Bearer {k}"}, payload, tout)
    if code >= 400 or not jd:
        raise RuntimeError(f"openai_{code}:{raw[:200]}")
    txt = ""
    try:
        ch = jd.get("choices") or []
        if ch:
            txt = str(((ch[0] or {}).get("message") or {}).get("content") or "").strip()
    except Exception:
        txt = ""
    return {"text": txt}


def _gemini_json(user: str, mime: str, b64: str, tout: float) -> dict[str, Any]:
    k = str(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not k:
        raise RuntimeError("no_gemini_key")
    md = str(os.getenv("POST_VLM_GEMINI_MODEL") or "gemini-2.0-flash").strip()
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(md)}:generateContent?key={urllib.parse.quote(k)}"
    )
    body = {
        "systemInstruction": {"parts": [{"text": _SYSTEM}]},
        "contents": [
            {"role": "user", "parts": [{"text": user}, {"inline_data": {"mime_type": mime, "data": b64}}]}
        ],
        "generationConfig": {"temperature": 0.08, "responseMimeType": "application/json"},
    }
    code, jd, raw = _https_json(endpoint, {}, body, tout)
    if code >= 400 or not jd:
        raise RuntimeError(f"gemini_{code}:{raw[:200]}")
    txt = ""
    try:
        c0 = (jd.get("candidates") or [{}])[0] or {}
        content = c0.get("content") if isinstance(c0.get("content"), dict) else {}
        parts = (content.get("parts") or []) if isinstance(content, dict) else []
        if parts and isinstance(parts[0], dict):
            txt = str(parts[0].get("text") or "").strip()
    except Exception:
        txt = ""
    return {"text": txt}
