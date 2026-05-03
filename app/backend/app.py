from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.backend.config import load_settings
from app.backend.db.sqlite import connect, migrate
from app.backend.logging_service import JobLogger, LogPaths
from app.backend.queue.repo import JobRepo, LogRepo
from app.utils.paths import ensure_dir, repo_root

_PC_DATA_ROOT = (repo_root() / "post_classifier_data").resolve()
_PC_UNCERTAIN_DIR = (_PC_DATA_ROOT / "_uncertain").resolve()
_PC_POS_DIR = (_PC_DATA_ROOT / "positive").resolve()
_PC_NEG_DIR = (_PC_DATA_ROOT / "negative").resolve()


def _safe_uncertain_rel(rel: str) -> Path:
    """
    Allow only files under post_classifier_data/_uncertain/** with supported image extensions.
    rel is a posix-like relative path, e.g. "pos_like/xxx.png".
    """
    s = (rel or "").strip().replace("\\", "/")
    if not s or any(x in s for x in ["..", "\x00"]) or s.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    p = (_PC_UNCERTAIN_DIR / s).resolve()
    if _PC_UNCERTAIN_DIR not in p.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    return p


def _safe_dataset_filename(name: str) -> str:
    n = (name or "").strip().replace("\\", "/").split("/")[-1]
    if not n:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if any(x in n for x in ["..", "\x00"]) or n.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if Path(n).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    return n

settings = load_settings()

conn = connect(settings.sqlite_path)
migrate(conn)

job_repo = JobRepo(conn)
log_repo = LogRepo(conn)
logger = JobLogger(log_repo, LogPaths(logs_root=ensure_dir(repo_root() / "app" / "logs")))

app = FastAPI(title="FB Posts Screenshot Tool", version="1.0.0")

# Optional: background post-classifier training (triggered from UI)
_TRAIN_PROC: subprocess.Popen | None = None
_TRAIN_STARTED_AT: float | None = None

_STATE_KEY_WORKER_COUNT = "worker_count"
_STATE_KEY_MAX_KEYWORDS = "max_keywords"
_STATE_KEY_HEADLESS = "headless"
_STATE_KEY_LIMIT_ENABLED = "limit_enabled"
_STATE_KEY_MAX_POSTS = "max_posts"
_STATE_KEY_KEYWORD_FILE = "keyword_file"
_STATE_KEY_EMAIL = "email"
_STATE_KEY_SAVE_SECRETS_TO_DOTENV = "save_secrets_to_dotenv"
_STATE_KEY_DELAY_MIN_SEC = "delay_min_sec"
_STATE_KEY_DELAY_MAX_SEC = "delay_max_sec"
_STATE_KEY_BETWEEN_KW_DELAY_MIN_SEC = "between_kw_delay_min_sec"
_STATE_KEY_BETWEEN_KW_DELAY_MAX_SEC = "between_kw_delay_max_sec"


def _get_state_int(key: str, default: int) -> int:
    try:
        row = conn.execute("SELECT value FROM worker_state WHERE key=?;", (key,)).fetchone()
        if not row:
            return int(default)
        v = str(row["value"] or "").strip()
        n = int(v)
        return n
    except Exception:
        return int(default)


def _set_state_int(key: str, value: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO worker_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value;",
            (key, str(int(value))),
        )


def _get_state_str(key: str, default: str) -> str:
    try:
        row = conn.execute("SELECT value FROM worker_state WHERE key=?;", (key,)).fetchone()
        if not row:
            return str(default)
        return str(row["value"] or "")
    except Exception:
        return str(default)


def _set_state_str(key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO worker_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value;",
            (key, str(value)),
        )


def _get_state_bool(key: str, default: bool) -> bool:
    v = _get_state_str(key, "1" if default else "0").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _set_state_bool(key: str, value: bool) -> None:
    _set_state_str(key, "1" if bool(value) else "0")


def _dotenv_path() -> Path:
    return (repo_root() / "app" / ".env").resolve()


def _update_dotenv_keys(updates: dict[str, str]) -> None:
    """
    Lightweight .env writer: preserves unrelated lines; updates keys by rewrite.
    """
    p = _dotenv_path()
    existing = ""
    if p.exists():
        try:
            existing = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            existing = p.read_text(encoding="utf-8", errors="replace")

    lines = existing.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in ln:
            out.append(ln)
            continue
        k = ln.split("=", 1)[0].strip()
        if k in updates:
            seen.add(k)
            out.append(f"{k}={updates[k]}")
        else:
            out.append(ln)

    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _frontend_dir() -> Path:
    return (repo_root() / "app" / "frontend").resolve()


def _keywords_dir() -> Path:
    # Folder name per user requirement
    return (repo_root() / "keyword").resolve()


def _safe_keyword_filename(name: str) -> str:
    # Prevent path traversal; allow only simple filenames ending with .txt
    n = (name or "").strip().replace("\\", "/").split("/")[-1]
    if not n.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="Only .txt files are allowed")
    if any(x in n for x in ["..", "\x00"]) or n.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return n


@app.get("/settings")
def get_settings() -> JSONResponse:
    worker_count = _get_state_int(_STATE_KEY_WORKER_COUNT, int(getattr(settings, "default_worker_count", 1)))
    max_keywords = _get_state_int(_STATE_KEY_MAX_KEYWORDS, int(getattr(settings, "default_max_keywords", 500)))
    # clamp
    worker_count = max(1, min(8, int(worker_count)))
    max_keywords = max(1, min(5000, int(max_keywords)))
    headless = _get_state_bool(_STATE_KEY_HEADLESS, False)
    limit_enabled = _get_state_bool(_STATE_KEY_LIMIT_ENABLED, False)
    max_posts = _get_state_int(_STATE_KEY_MAX_POSTS, int(getattr(settings, "default_max_posts", 30)))
    max_posts = max(1, min(20000, int(max_posts)))
    keyword_file = _get_state_str(_STATE_KEY_KEYWORD_FILE, "").strip()
    email = _get_state_str(_STATE_KEY_EMAIL, "").strip()
    save_secrets_to_dotenv = _get_state_bool(_STATE_KEY_SAVE_SECRETS_TO_DOTENV, False)
    delay_min_sec = float(_get_state_str(_STATE_KEY_DELAY_MIN_SEC, str(getattr(settings, "default_delay_min_sec", 1.0))) or "1")
    delay_max_sec = float(_get_state_str(_STATE_KEY_DELAY_MAX_SEC, str(getattr(settings, "default_delay_max_sec", 3.0))) or "3")
    bkw_min_sec = float(
        _get_state_str(
            _STATE_KEY_BETWEEN_KW_DELAY_MIN_SEC,
            str(getattr(settings, "default_between_keywords_delay_min_sec", 1.0)),
        )
        or "1"
    )
    bkw_max_sec = float(
        _get_state_str(
            _STATE_KEY_BETWEEN_KW_DELAY_MAX_SEC,
            str(getattr(settings, "default_between_keywords_delay_max_sec", 2.0)),
        )
        or "2"
    )
    return JSONResponse(
        {
            "workerCount": worker_count,
            "maxKeywords": max_keywords,
            "headless": headless,
            "limitEnabled": limit_enabled,
            "maxPosts": max_posts,
            "keywordFile": keyword_file,
            "email": email,
            "saveSecretsToDotenv": save_secrets_to_dotenv,
            "delayMinSec": delay_min_sec,
            "delayMaxSec": delay_max_sec,
            "betweenKwDelayMinSec": bkw_min_sec,
            "betweenKwDelayMaxSec": bkw_max_sec,
        }
    )


@app.post("/settings")
def set_settings(payload: dict) -> JSONResponse:
    try:
        wc_raw = payload.get("workerCount", None)
        mk_raw = payload.get("maxKeywords", None)

        wc = _get_state_int(_STATE_KEY_WORKER_COUNT, int(getattr(settings, "default_worker_count", 1)))
        mk = _get_state_int(_STATE_KEY_MAX_KEYWORDS, int(getattr(settings, "default_max_keywords", 500)))

        if wc_raw is not None:
            wc = int(wc_raw)
        if mk_raw is not None:
            mk = int(mk_raw)

        if wc < 1 or wc > 8:
            raise ValueError("workerCount không hợp lệ (1..8)")
        if mk < 1 or mk > 5000:
            raise ValueError("maxKeywords không hợp lệ (1..5000)")

        headless = bool(payload.get("headless", _get_state_bool(_STATE_KEY_HEADLESS, False)))
        limit_enabled = bool(payload.get("limitEnabled", _get_state_bool(_STATE_KEY_LIMIT_ENABLED, False)))

        mp_raw = payload.get("maxPosts", None)
        mp = _get_state_int(_STATE_KEY_MAX_POSTS, int(getattr(settings, "default_max_posts", 30)))
        if mp_raw is not None:
            mp = int(mp_raw)
        mp = max(1, min(20000, int(mp)))

        keyword_file_raw = payload.get("keywordFile", None)
        keyword_file = _get_state_str(_STATE_KEY_KEYWORD_FILE, "").strip()
        if keyword_file_raw is not None:
            keyword_file = str(keyword_file_raw or "").strip()
        if keyword_file:
            safe = _safe_keyword_filename(keyword_file)
            p = (_keywords_dir() / safe).resolve()
            d = _keywords_dir().resolve()
            if d not in p.parents and p != d:
                raise ValueError("keywordFile không hợp lệ")
            if not p.exists() or not p.is_file():
                raise ValueError("keywordFile không tồn tại trong folder keyword/")
            keyword_file = safe

        email = str(payload.get("email", _get_state_str(_STATE_KEY_EMAIL, ""))).strip()
        password = str(payload.get("password", ""))
        save_secrets_to_dotenv = bool(
            payload.get("saveSecretsToDotenv", _get_state_bool(_STATE_KEY_SAVE_SECRETS_TO_DOTENV, False))
        )
        if save_secrets_to_dotenv and password.strip() == "":
            raise ValueError("Bật “Lưu FB_EMAIL/FB_PASSWORD vào app/.env” nhưng đang để trống password.")

        # Speed / cooldown settings (seconds)
        dmin_raw = payload.get("delayMinSec", None)
        dmax_raw = payload.get("delayMaxSec", None)
        bmin_raw = payload.get("betweenKwDelayMinSec", None)
        bmax_raw = payload.get("betweenKwDelayMaxSec", None)

        dmin = float(_get_state_str(_STATE_KEY_DELAY_MIN_SEC, str(getattr(settings, "default_delay_min_sec", 1.0))) or "1")
        dmax = float(_get_state_str(_STATE_KEY_DELAY_MAX_SEC, str(getattr(settings, "default_delay_max_sec", 3.0))) or "3")
        bmin = float(
            _get_state_str(
                _STATE_KEY_BETWEEN_KW_DELAY_MIN_SEC,
                str(getattr(settings, "default_between_keywords_delay_min_sec", 1.0)),
            )
            or "1"
        )
        bmax = float(
            _get_state_str(
                _STATE_KEY_BETWEEN_KW_DELAY_MAX_SEC,
                str(getattr(settings, "default_between_keywords_delay_max_sec", 2.0)),
            )
            or "2"
        )

        if dmin_raw is not None:
            dmin = float(dmin_raw)
        if dmax_raw is not None:
            dmax = float(dmax_raw)
        if bmin_raw is not None:
            bmin = float(bmin_raw)
        if bmax_raw is not None:
            bmax = float(bmax_raw)

        def _clamp_delay(v: float, lo: float, hi: float, name: str) -> float:
            if v != v:  # NaN
                raise ValueError(f"{name} không hợp lệ")
            if v < lo or v > hi:
                raise ValueError(f"{name} không hợp lệ ({lo}..{hi} giây)")
            return float(v)

        dmin = _clamp_delay(dmin, 0.0, 20.0, "delayMinSec")
        dmax = _clamp_delay(dmax, 0.0, 20.0, "delayMaxSec")
        bmin = _clamp_delay(bmin, 0.0, 60.0, "betweenKwDelayMinSec")
        bmax = _clamp_delay(bmax, 0.0, 60.0, "betweenKwDelayMaxSec")
        if dmax < dmin:
            raise ValueError("delayMaxSec phải >= delayMinSec")
        if bmax < bmin:
            raise ValueError("betweenKwDelayMaxSec phải >= betweenKwDelayMinSec")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    _set_state_int(_STATE_KEY_WORKER_COUNT, wc)
    _set_state_int(_STATE_KEY_MAX_KEYWORDS, mk)
    _set_state_bool(_STATE_KEY_HEADLESS, headless)
    _set_state_bool(_STATE_KEY_LIMIT_ENABLED, limit_enabled)
    if limit_enabled:
        _set_state_int(_STATE_KEY_MAX_POSTS, mp)
    _set_state_str(_STATE_KEY_KEYWORD_FILE, keyword_file)
    _set_state_str(_STATE_KEY_EMAIL, email)
    _set_state_bool(_STATE_KEY_SAVE_SECRETS_TO_DOTENV, save_secrets_to_dotenv)
    _set_state_str(_STATE_KEY_DELAY_MIN_SEC, str(dmin))
    _set_state_str(_STATE_KEY_DELAY_MAX_SEC, str(dmax))
    _set_state_str(_STATE_KEY_BETWEEN_KW_DELAY_MIN_SEC, str(bmin))
    _set_state_str(_STATE_KEY_BETWEEN_KW_DELAY_MAX_SEC, str(bmax))

    if save_secrets_to_dotenv:
        updates = {}
        updates["FB_EMAIL"] = email
        updates["FB_PASSWORD"] = password
        _update_dotenv_keys(updates)
        # Make current process see updated values immediately (worker may read via jobs anyway).
        os.environ["FB_EMAIL"] = email
        os.environ["FB_PASSWORD"] = password

    return JSONResponse(
        {
            "ok": True,
            "workerCount": wc,
            "maxKeywords": mk,
            "headless": headless,
            "limitEnabled": limit_enabled,
            "maxPosts": mp,
            "keywordFile": keyword_file,
            "email": email,
            "saveSecretsToDotenv": save_secrets_to_dotenv,
            "delayMinSec": dmin,
            "delayMaxSec": dmax,
            "betweenKwDelayMinSec": bmin,
            "betweenKwDelayMaxSec": bmax,
        }
    )


frontend_dir = _frontend_dir()
app.mount("/assets", StaticFiles(directory=str(frontend_dir / "assets")), name="assets")


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    p = frontend_dir / "index.html"
    return FileResponse(str(p))


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/post-classifier/status")
def post_classifier_status() -> JSONResponse:
    from app.worker.post_classifier.train_status import build_public_status

    global _TRAIN_PROC, _TRAIN_STARTED_AT
    proc_running = False
    pid = None
    started_at = None
    try:
        if _TRAIN_PROC is not None and _TRAIN_PROC.poll() is None:
            proc_running = True
            pid = int(getattr(_TRAIN_PROC, "pid", 0) or 0) or None
            started_at = _TRAIN_STARTED_AT
        else:
            _TRAIN_PROC = None
            _TRAIN_STARTED_AT = None
    except Exception:
        _TRAIN_PROC = None
        _TRAIN_STARTED_AT = None

    data = build_public_status()
    data["server"] = {
        "trainProcessRunning": bool(proc_running),
        "pid": pid,
        "startedAtUnix": started_at,
    }
    return JSONResponse(data)


@app.post("/post-classifier/train")
def post_classifier_train() -> JSONResponse:
    """
    Trigger background training from the UI.
    Safe: does not block the server; rejects if a train process is already running.
    """
    global _TRAIN_PROC, _TRAIN_STARTED_AT
    if _TRAIN_PROC is not None:
        try:
            if _TRAIN_PROC.poll() is None:
                return JSONResponse({"ok": False, "running": True, "pid": int(getattr(_TRAIN_PROC, "pid", 0) or 0)})
        except Exception:
            _TRAIN_PROC = None
            _TRAIN_STARTED_AT = None

    # Run in a separate process so the backend stays responsive.
    # Use current interpreter (venv) since supervisor launches backend with sys.executable.
    try:
        _TRAIN_STARTED_AT = time.time()
        _TRAIN_PROC = subprocess.Popen(
            [sys.executable, "-m", "app.worker.post_classifier.train"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(repo_root()),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        return JSONResponse({"ok": True, "running": True, "pid": int(getattr(_TRAIN_PROC, "pid", 0) or 0)})
    except Exception as e:
        _TRAIN_PROC = None
        _TRAIN_STARTED_AT = None
        raise HTTPException(status_code=500, detail=f"Cannot start training: {e}")


@app.get("/post-classifier/uncertain/list")
def post_classifier_uncertain_list(limit: int = Query(120, ge=1, le=500)) -> JSONResponse:
    """
    List uncertain samples collected by worker (active learning).
    """
    items: list[dict] = []
    base = _PC_UNCERTAIN_DIR
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    try:
        # Newest first for UX.
        files = [p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in exts]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[: int(limit)]:
            rel = p.relative_to(base).as_posix()
            bucket = rel.split("/", 1)[0] if "/" in rel else ""
            items.append(
                {
                    "rel": rel,
                    "bucket": bucket,
                    "name": p.name,
                    "modifiedAt": p.stat().st_mtime,
                    "url": f"/post-classifier/uncertain/file?rel={rel}",
                }
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot list uncertain: {e}")
    return JSONResponse({"ok": True, "count": len(items), "items": items})


@app.get("/post-classifier/uncertain/file")
def post_classifier_uncertain_file(rel: str = Query(...)) -> FileResponse:
    p = _safe_uncertain_rel(rel)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(p))


@app.post("/post-classifier/uncertain/move")
def post_classifier_uncertain_move(payload: dict) -> JSONResponse:
    """
    Move an uncertain file to positive/negative dataset.
    payload: { rel: "bucket/file.png", target: "positive"|"negative" }
    """
    rel = str(payload.get("rel", "")).strip()
    target = str(payload.get("target", "")).strip().lower()
    if target not in {"positive", "negative"}:
        raise HTTPException(status_code=400, detail="Invalid target")
    src = _safe_uncertain_rel(rel)
    if not src.exists():
        raise HTTPException(status_code=404, detail="Not found")
    dst_dir = _PC_POS_DIR if target == "positive" else _PC_NEG_DIR
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    dst_name = _safe_dataset_filename(src.name)
    dst = (dst_dir / dst_name).resolve()
    # Avoid overwrite: append suffix if needed.
    if dst.exists():
        stem = dst.stem
        suf = dst.suffix
        i = 2
        while True:
            cand = (dst_dir / f"{stem}_{i}{suf}").resolve()
            if not cand.exists():
                dst = cand
                break
            i += 1
            if i > 999:
                raise HTTPException(status_code=409, detail="Too many duplicates")
    try:
        src.replace(dst)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot move file: {e}")
    return JSONResponse({"ok": True, "movedTo": str(dst), "target": target})


@app.post("/post-classifier/uncertain/delete")
def post_classifier_uncertain_delete(payload: dict) -> JSONResponse:
    rel = str(payload.get("rel", "")).strip()
    p = _safe_uncertain_rel(rel)
    if not p.exists():
        return JSONResponse({"ok": True, "deleted": False})
    try:
        p.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot delete: {e}")
    return JSONResponse({"ok": True, "deleted": True})


@app.get("/keywords/files")
def keyword_files() -> JSONResponse:
    d = _keywords_dir()
    d.mkdir(parents=True, exist_ok=True)
    files = sorted([p.name for p in d.glob("*.txt") if p.is_file()])
    return JSONResponse({"files": files})


@app.get("/keywords/file")
def keyword_file(name: str = Query(...)) -> JSONResponse:
    d = _keywords_dir()
    d.mkdir(parents=True, exist_ok=True)
    safe = _safe_keyword_filename(name)
    p = (d / safe).resolve()
    if d not in p.parents and p != d:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        txt = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        txt = p.read_text(encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    return JSONResponse({"name": safe, "keywords": lines, "count": len(lines)})


@app.post("/start-job")
def start_job(payload: dict) -> JSONResponse:
    try:
        email = str(payload.get("email", "")).strip()
        password = str(payload.get("password", ""))
        keywords_raw = payload.get("keywords", [])
        saved_headless = _get_state_bool(_STATE_KEY_HEADLESS, False)
        headless = bool(payload.get("headless", saved_headless))
        # max_posts=0 => unlimited (capture until end of results)
        saved_limit = _get_state_bool(_STATE_KEY_LIMIT_ENABLED, False)
        saved_max_posts = _get_state_int(_STATE_KEY_MAX_POSTS, int(getattr(settings, "default_max_posts", 30)))
        saved_max_posts = max(1, min(20000, int(saved_max_posts)))
        limit_enabled = bool(payload.get("limitEnabled", saved_limit))
        max_posts_raw = payload.get("maxPosts", (saved_max_posts if limit_enabled else 0))
        try:
            max_posts = int(max_posts_raw or 0)
        except Exception:
            max_posts = 0
        if max_posts < 0:
            max_posts = 0
        if max_posts > 20000:
            raise ValueError("maxPosts quá lớn (tối đa 20000)")

        # Delays (cooldown) are configured via /settings (stored in worker_state).
        delay_min = float(
            _get_state_str(_STATE_KEY_DELAY_MIN_SEC, str(getattr(settings, "default_delay_min_sec", 1.0))) or "1"
        )
        delay_max = float(
            _get_state_str(_STATE_KEY_DELAY_MAX_SEC, str(getattr(settings, "default_delay_max_sec", 3.0))) or "3"
        )
        bmin = float(
            _get_state_str(
                _STATE_KEY_BETWEEN_KW_DELAY_MIN_SEC,
                str(getattr(settings, "default_between_keywords_delay_min_sec", 1.0)),
            )
            or "1"
        )
        bmax = float(
            _get_state_str(
                _STATE_KEY_BETWEEN_KW_DELAY_MAX_SEC,
                str(getattr(settings, "default_between_keywords_delay_max_sec", 2.0)),
            )
            or "2"
        )


        # Allow credentials from .env when UI leaves blank
        if not email:
            email = _get_state_str(_STATE_KEY_EMAIL, "").strip()
        if not email:
            email = (os.getenv("FB_EMAIL") or "").strip()
        if not password:
            password = os.getenv("FB_PASSWORD") or ""

        # Per user requirement: store plaintext password (not encrypted).
        if not email or not password:
            raise ValueError("Missing email/password (provide in UI or .env via FB_EMAIL + FB_PASSWORD)")

        if not isinstance(keywords_raw, list):
            raise ValueError("keywords must be list")
        keywords = [str(k).strip() for k in keywords_raw if str(k).strip()]
        if not keywords:
            kwf = _get_state_str(_STATE_KEY_KEYWORD_FILE, "").strip()
            if not kwf:
                raise ValueError("No keywords")
            safe = _safe_keyword_filename(kwf)
            p = (_keywords_dir() / safe).resolve()
            d = _keywords_dir().resolve()
            if d not in p.parents and p != d:
                raise ValueError("Invalid keyword file path")
            if not p.exists() or not p.is_file():
                raise ValueError("Keyword file not found")
            try:
                txt = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                txt = p.read_text(encoding="utf-8", errors="replace")
            keywords = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            if not keywords:
                raise ValueError("No keywords")

        # Enforce max keywords (user-configurable in UI).
        max_keywords = _get_state_int(
            _STATE_KEY_MAX_KEYWORDS, int(getattr(settings, "default_max_keywords", 500))
        )
        max_keywords = max(1, min(5000, int(max_keywords)))
        truncated = 0
        if len(keywords) > max_keywords:
            truncated = len(keywords) - max_keywords
            keywords = keywords[:max_keywords]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # By default, do NOT hard-assign jobs to specific workers.
    # Hard assignment can cause idle workers (e.g. w2) while pending jobs are stuck on w0/w1.
    # Enable strict assignment with ASSIGN_JOBS_TO_WORKERS=1 if you really want deterministic sharding.
    assign_jobs = (os.getenv("ASSIGN_JOBS_TO_WORKERS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
    assigned_worker_ids = None
    if assign_jobs:
        wc = max(1, int(_get_state_int(_STATE_KEY_WORKER_COUNT, 1)))
        assigned_worker_ids = [(i % wc) for i in range(len(keywords))]

    job_ids = job_repo.create_jobs(
        email=email,
        password_enc=password,
        keywords=keywords,
        assigned_worker_ids=assigned_worker_ids,
        headless=headless,
        max_posts=max_posts,
        delay_min_sec=delay_min,
        delay_max_sec=delay_max,
        between_keywords_delay_min_sec=bmin,
        between_keywords_delay_max_sec=bmax,
        max_attempts=1,
    )
    for jid, kw in zip(job_ids, keywords):
        logger.log(
            job_id=jid,
            keyword=kw,
            level="INFO",
            step="created",
            message="Job created",
            data={"maxPosts": ("unlimited" if max_posts <= 0 else max_posts)},
        )

    if truncated:
        # Lightweight notice on the first created job (avoid spamming N logs).
        try:
            logger.log(
                job_id=job_ids[0],
                keyword=keywords[0],
                level="WARN",
                step="created",
                message=f"Đã cắt danh sách keyword: bỏ qua {truncated} keyword cuối do maxKeywords={max_keywords}.",
                data={"totalBefore": len(keywords) + truncated, "maxKeywords": max_keywords},
            )
        except Exception:
            pass
    return JSONResponse({"jobIds": job_ids, "maxKeywords": max_keywords, "truncated": int(truncated)})


@app.post("/checkpoint/decision")
def checkpoint_decision(payload: dict) -> JSONResponse:
    """
    UI confirms what to do when a worker detects checkpoint/captcha.
    decision: "reload" | "continue"
    """
    job_id = str(payload.get("jobId", "")).strip()
    decision = str(payload.get("decision", "")).strip().lower()
    if not job_id:
        raise HTTPException(status_code=400, detail="Missing jobId")
    if decision not in {"reload", "continue"}:
        raise HTTPException(status_code=400, detail="decision must be reload|continue")
    job = job_repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job_repo.set_checkpoint_decision(job_id, decision)
    return JSONResponse({"ok": True, "jobId": job_id, "decision": decision})


@app.get("/job-status")
def job_status() -> JSONResponse:
    return JSONResponse({"jobs": job_repo.list_jobs()})


@app.get("/logs")
def logs(jobId: str = Query(...), offset: int = Query(0, ge=0), limit: int = Query(300, ge=1, le=1000)):
    items = log_repo.list_after(jobId, offset_seq=offset, limit=limit)
    return JSONResponse({"items": items})


async def _sse_event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/logs/stream")
async def logs_stream(jobId: str = Query(...), offset: int = Query(0, ge=0)) -> StreamingResponse:
    async def gen() -> AsyncGenerator[bytes, None]:
        last = offset
        # simple polling SSE; production: can be replaced by notify/event bus
        while True:
            items = log_repo.list_after(jobId, offset_seq=last, limit=300)
            for it in items:
                last = int(it["seq"])
                payload = {
                    "seq": it["seq"],
                    "ts": it["ts"],
                    "level": it["level"],
                    "jobId": it["job_id"],
                    "keyword": it["keyword"],
                    "step": it.get("step") or "",
                    "message": it["message"],
                    "data": it.get("data_json"),
                }
                yield (await _sse_event(payload)).encode("utf-8")
            await asyncio.sleep(0.7)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/stop")
def stop(payload: dict) -> JSONResponse:
    job_id: Optional[str] = payload.get("jobId")
    stop_all = bool(payload.get("all", False))
    if stop_all:
        n = job_repo.request_cancel_all()
        return JSONResponse({"ok": True, "cancelled": n})
    if job_id:
        ok = job_repo.request_cancel(str(job_id))
        return JSONResponse({"ok": ok, "cancelled": 1 if ok else 0})
    raise HTTPException(status_code=400, detail="Provide jobId or all=true")


@app.post("/clean")
def clean(payload: dict) -> JSONResponse:
    """
    Remove all jobs and logs from the local SQLite database.
    Intended for UI "Clean all" (reset dashboard history).
    """
    # If the worker is running, this is a hard reset; caller can press Stop All first.
    logs_deleted = log_repo.delete_all()
    jobs_deleted = job_repo.delete_all()
    return JSONResponse({"ok": True, "jobsDeleted": jobs_deleted, "logsDeleted": logs_deleted})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.backend.app:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=False,
        log_level="info",
    )

