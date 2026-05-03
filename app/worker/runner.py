from __future__ import annotations

import os
import random
import signal
import time
import traceback
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from app.backend.config import load_settings
from app.backend.db.sqlite import connect, migrate
from app.backend.logging_service import JobLogger, LogPaths
from app.backend.queue.repo import JobRepo, LogRepo
from app.utils.paths import ensure_dir, repo_root
from app.utils.nethealth import check_net_health

from app.worker.fb_automation.automation import (
    CaptchaOrCheckpointDetected,
    ElementTimeout,
    RunParams,
    capture_posts,
    ensure_home_logged_in,
    ensure_login,
    search_keyword,
)


def _rand(a: float, b: float) -> float:
    return random.uniform(a, b)


def _is_target_closed_error(e: BaseException) -> bool:
    msg = str(e).lower()
    return (
        "has been closed" in msg
        or "target page" in msg
        or "browser has been closed" in msg
        or ("context" in msg and "closed" in msg)
        or "connection closed" in msg
        or "driver" in msg and "closed" in msg
    )


class _SingleInstanceLock:
    """
    Prevent multiple workers from using the same chrome-profile concurrently.
    This avoids Playwright persistent context flapping/closing on Windows.
    """

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fh = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.lock_path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.seek(0)
            self._fh.truncate(0)
            self._fh.write(f"pid={os.getpid()}\n")
            self._fh.flush()
            return True
        except Exception:
            # If the lock is held by a dead process (common after crashes/forced kills),
            # try to remove stale lock content and retry once.
            existing = ""
            try:
                self._fh.seek(0)
                existing = self._fh.read() or ""
            except Exception:
                existing = ""

            def _pid_exists(pid: int) -> bool:
                if pid <= 0:
                    return False
                if os.name != "nt":
                    try:
                        os.kill(pid, 0)
                        return True
                    except Exception:
                        return False
                try:
                    import ctypes

                    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
                    if handle:
                        ctypes.windll.kernel32.CloseHandle(handle)
                        return True
                    return False
                except Exception:
                    return True  # be conservative

            pid = None
            try:
                if existing.strip().startswith("pid="):
                    pid = int(existing.strip().split("pid=", 1)[1].splitlines()[0].strip())
            except Exception:
                pid = None

            if pid is not None and not _pid_exists(pid):
                # Process is gone; try to clear and re-lock.
                try:
                    self._fh.seek(0)
                    self._fh.truncate(0)
                    self._fh.flush()
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
                # Retry once (fresh file handle)
                try:
                    self._fh = self.lock_path.open("a+", encoding="utf-8")
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fh.seek(0)
                    self._fh.truncate(0)
                    self._fh.write(f"pid={os.getpid()}\n")
                    self._fh.flush()
                    return True
                except Exception:
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                    self._fh = None
                    return False

            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            return False

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None


class _ProfileDirLock:
    """
    Lock the chrome profile directory so user cannot open Chrome with same userDataDir
    while automation is running. On Windows, profile contention can cause the browser
    to close immediately, producing "Target page/context/browser has been closed".
    """

    def __init__(self, profile_dir: Path):
        self.profile_dir = profile_dir
        self.lock_path = profile_dir / ".profile.lock"
        self._fh = None

    def acquire(self) -> bool:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._fh = self.lock_path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.seek(0)
            self._fh.truncate(0)
            self._fh.write(f"pid={os.getpid()}\n")
            self._fh.flush()
            return True
        except Exception:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            return False

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None


def _get_worker_id() -> int:
    raw = os.environ.get("WORKER_ID", "").strip()
    if not raw:
        return 0
    try:
        wid = int(raw)
    except Exception:
        return 0
    return max(0, wid)


def _worker_state_bool(conn, key: str, default: bool) -> bool:
    """Read boolean from SQLite worker_state (matches app/backend/app.py conventions)."""
    try:
        row = conn.execute("SELECT value FROM worker_state WHERE key=?;", (key,)).fetchone()
        if not row:
            return bool(default)
        v = str(row["value"] or "").strip().lower()
        return v in {"1", "true", "yes", "on"}
    except Exception:
        return bool(default)


def _worker_profile_dir(base: Path, worker_id: int) -> Path:
    # Use per-worker userDataDir to allow multiple Playwright persistent contexts in parallel.
    # Keeping worker_id=0 on the base dir preserves backwards compatibility.
    if worker_id <= 0:
        return base
    return base.parent / f"{base.name}-w{worker_id}"


def main() -> None:
    settings = load_settings()
    conn = connect(settings.sqlite_path)
    migrate(conn)

    job_repo = JobRepo(conn)
    log_repo = LogRepo(conn)
    logger = JobLogger(log_repo, LogPaths(logs_root=ensure_dir(repo_root() / "app" / "logs")))

    posts_root = ensure_dir(repo_root() / "posts")
    worker_id = _get_worker_id()
    profile_dir = _worker_profile_dir(settings.chrome_profile_dir, worker_id)
    ensure_dir(profile_dir)

    # Ensure only ONE worker instance runs per WORKER_ID/profile.
    lock = _SingleInstanceLock(repo_root() / "app" / "storage" / f"worker-w{worker_id}.lock")
    if not lock.acquire():
        print(f"Worker w{worker_id} is already running. Close it and retry.")
        raise SystemExit(3)

    profile_lock = _ProfileDirLock(profile_dir)
    if not profile_lock.acquire():
        print(
            "Chrome profile is in use for this worker. Close ALL Chrome windows started by this tool "
            "and do not open the chrome-profile dir manually, then retry."
        )
        lock.release()
        raise SystemExit(4)

    try:
        with sync_playwright() as p:
            context = None
            page = None
            shutdown_requested = False
            last_page_url = ""

            def request_shutdown(reason: str) -> None:
                nonlocal shutdown_requested, context, page
                if shutdown_requested:
                    return
                shutdown_requested = True
                try:
                    print(f"[worker] Shutdown requested: {reason}")
                except Exception:
                    pass
                try:
                    if page is not None:
                        page.close()
                except Exception:
                    pass
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass

            def _sig_handler(signum, _frame):
                request_shutdown(f"signal={signum}")

            try:
                signal.signal(signal.SIGINT, _sig_handler)
                signal.signal(signal.SIGTERM, _sig_handler)
            except Exception:
                pass

            # Windows: ensure closing the console terminates the worker AND closes Chrome.
            if os.name == "nt":
                try:
                    import ctypes

                    CTRL_C_EVENT = 0
                    CTRL_BREAK_EVENT = 1
                    CTRL_CLOSE_EVENT = 2
                    CTRL_LOGOFF_EVENT = 5
                    CTRL_SHUTDOWN_EVENT = 6

                    HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

                    def _console_handler(ctrl_type: int) -> bool:
                        if ctrl_type in {
                            CTRL_C_EVENT,
                            CTRL_BREAK_EVENT,
                            CTRL_CLOSE_EVENT,
                            CTRL_LOGOFF_EVENT,
                            CTRL_SHUTDOWN_EVENT,
                        }:
                            request_shutdown(f"console_ctrl={ctrl_type}")
                            return True
                        return False

                    ctypes.windll.kernel32.SetConsoleCtrlHandler(HandlerRoutine(_console_handler), True)
                except Exception:
                    pass

            def relaunch() -> None:
                nonlocal context, page, last_page_url
                try:
                    if page is not None:
                        page.close()
                except Exception:
                    pass
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass

                def _launch(user_data_dir: Path, extra_args: list[str], label: str):
                    # Note: `job` is assigned before we call relaunch() for a claimed job.
                    # If relaunch happens outside a job, default to visible Chrome.
                    try:
                        is_headless = bool(int(getattr(job, "headless", 0)))
                    except Exception:
                        is_headless = False
                    keep_scroll_active = os.getenv("FB_KEEP_SCROLL_ACTIVE", "0").strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                    base_flags = [
                        "--disable-blink-features=AutomationControlled",
                        "--start-maximized",
                        "--force-device-scale-factor=1",
                        "--high-dpi-support=1",
                        "--disable-features=OverlayScrollbar",
                        "--disable-notifications",
                    ]
                    # When false (default): reduce focus-stealing, but FB can throttle scrolling/rendering
                    # when the automation window is in background — set FB_KEEP_SCROLL_ACTIVE=1 to prefer smooth scrolling.
                    if not keep_scroll_active:
                        base_flags.extend(
                            [
                                "--disable-backgrounding-occluded-windows",
                                "--disable-renderer-backgrounding",
                                "--disable-background-timer-throttling",
                            ]
                        )
                    launch_kwargs = dict(
                        user_data_dir=str(user_data_dir),
                        headless=is_headless,
                        # Taller viewport reduces "clipped" element screenshots for long posts.
                        viewport={"width": 1280, "height": 1800},
                        # Normal DPI / scale (default).
                        device_scale_factor=1,
                        reduced_motion="reduce",
                        timeout=45_000,
                        args=[
                            *base_flags,
                            *extra_args,
                        ],
                    )
                    try:
                        ctx = p.chromium.launch_persistent_context(**launch_kwargs, channel="chrome")
                        print(f"[worker] Launched browser: channel=chrome ({label})")
                        return ctx
                    except Exception as e:
                        ctx = p.chromium.launch_persistent_context(**launch_kwargs)
                        print(f"[worker] Launched browser: bundled chromium ({label}; chrome channel failed: {e})")
                        return ctx

                # Launch strategy:
                # 1) Use the main persistent profile dir
                # 2) If launch fails/hangs due to GPU/driver/AV, retry with safer flags
                # 3) If profile is corrupted/locked, fall back to a new profile dir
                try:
                    context = _launch(profile_dir, [], f"profile=w{worker_id}")
                except Exception as e1:
                    print(f"[worker] Browser launch failed (profile w{worker_id}). Retrying with safe flags. ({e1})")
                    safe = ["--disable-gpu", "--disable-software-rasterizer", "--disable-dev-shm-usage"]
                    try:
                        context = _launch(profile_dir, safe, f"profile=w{worker_id}+safe")
                    except Exception as e2:
                        fb = profile_dir.parent / f"{profile_dir.name}-fallback"
                        fb.mkdir(parents=True, exist_ok=True)
                        print(
                            "[worker] Browser launch failed with main profile. "
                            f"Falling back to new profile dir: {fb} ({e2})"
                        )
                        context = _launch(fb, safe, f"profile=w{worker_id}-fallback+safe")

                # Stabilize screenshots: Facebook sometimes leaves font requests pending forever,
                # causing Page.screenshot to wait on "fonts to load..." until timeout.
                # Aborting fonts is safe for our use-case (visual content capture) and improves speed.
                try:
                    context.unroute("**/*")
                except Exception:
                    pass

                def _route_block_fonts(route, request):
                    try:
                        if request.resource_type == "font":
                            return route.abort()
                    except Exception:
                        pass
                    return route.continue_()

                try:
                    context.route("**/*", _route_block_fonts)
                except Exception:
                    pass

                # Persistent context usually opens one initial page (about:blank).
                # Do NOT create a second blank tab unnecessarily.
                pages = list(context.pages)
                if not pages:
                    page = context.new_page()
                    pages = [page]

                def _norm(u: str) -> str:
                    try:
                        return (u or "").strip().lower()
                    except Exception:
                        return ""

                # Pick the most likely "main" page for automation.
                # On Windows, persistent contexts sometimes restore extra tabs (extensions, crash restore, new-tab),
                # which can cause the worker to "operate" on the wrong tab (no scrolling / no posts visible).
                chosen = None
                try:
                    # 1) Prefer an existing Facebook tab if any.
                    for pg in pages:
                        u = _norm(getattr(pg, "url", "") or "")
                        if "facebook.com" in u:
                            chosen = pg
                            break
                    # 2) Prefer the last known URL if it still exists.
                    if chosen is None and last_page_url:
                        want = _norm(last_page_url)
                        for pg in pages:
                            if _norm(getattr(pg, "url", "") or "") == want:
                                chosen = pg
                                break
                    # 3) Otherwise keep the first page.
                    if chosen is None:
                        chosen = pages[0]
                except Exception:
                    chosen = pages[0]

                page = chosen

                # Close extra pages to enforce 1-tab-per-worker, reducing "ghost tabs" and focus issues.
                for extra in pages:
                    if extra is page:
                        continue
                    try:
                        extra.close()
                    except Exception:
                        pass

                # If browser instantly closes (profile contention / policy), fail fast with clear message.
                time.sleep(0.6)
                if page is None or (hasattr(page, "is_closed") and page.is_closed()):
                    raise RuntimeError(
                        "Browser closed immediately. Likely chrome-profile is locked by another Chrome process "
                        "or security software blocked automation."
                    )

            print(f"Worker w{worker_id} started. Waiting for jobs...")
            while True:
                if shutdown_requested:
                    raise SystemExit(0)
                job = job_repo.claim_next_pending(worker_id)
                if not job:
                    time.sleep(1.0)
                    continue

                keyword = job.keyword
                job_id = job.id

                def jlog(step: str, message: str, level: str = "INFO", data=None):
                    merged = {"w": worker_id}
                    if isinstance(data, dict):
                        merged.update(data)
                    logger.log(
                        job_id=job_id,
                        keyword=keyword,
                        level=level,
                        step=step,
                        message=message,
                        data=merged,
                    )

                def progress(cur: int, total: int):
                    job_repo.mark_progress(job_id, cur, total)

                def should_cancel() -> bool:
                    return job_repo.is_cancel_requested(job_id)

                # Watchdog (no-progress based): stop only if the worker makes NO progress for too long.
                # Do NOT use an absolute runtime timeout; long keywords can legitimately run >15 minutes.
                no_progress_timeout_s = float(os.getenv("FB_NO_PROGRESS_TIMEOUT_SEC", "420").strip() or "420")
                job_t0 = time.time()
                watchdog_tripped = {"timeout": False}
                last_hb_at = {"t": 0.0}
                last_progress_at = {"t": time.time()}

                def progress(cur: int, total: int):
                    # Update DB progress + watchdog progress timestamp.
                    try:
                        last_progress_at["t"] = time.time()
                    except Exception:
                        pass
                    job_repo.mark_progress(job_id, cur, total)

                def should_cancel_watchdog() -> bool:
                    # user-requested cancel still wins
                    try:
                        if should_cancel():
                            return True
                    except Exception:
                        pass
                    # heartbeat (cheap) so UI/logs show the worker is alive during long capture loops
                    now = time.time()
                    if last_hb_at["t"] <= 0.0:
                        last_hb_at["t"] = now
                    if now - last_hb_at["t"] >= 15.0:
                        last_hb_at["t"] = now
                        try:
                            jlog("heartbeat", "Worker alive", "INFO", data={"elapsed_s": int(now - job_t0)})
                        except Exception:
                            pass
                    # no-progress timeout
                    if no_progress_timeout_s > 0 and (now - float(last_progress_at["t"] or job_t0)) > no_progress_timeout_s:
                        watchdog_tripped["timeout"] = True
                        try:
                            jlog(
                                "watchdog",
                                f"No progress for too long ({int(no_progress_timeout_s)}s). Will stop this job to prevent hanging.",
                                "ERROR",
                                data={
                                    "elapsed_s": int(now - job_t0),
                                    "no_progress_timeout_s": int(no_progress_timeout_s),
                                    "since_last_progress_s": int(now - float(last_progress_at["t"] or job_t0)),
                                },
                            )
                        except Exception:
                            pass
                        return True
                    return False

                def net_guard(phase: str) -> float:
                    """
                    Detect slow/lag network and adapt timeouts so we don't fail early.
                    Returns a timeout multiplier (1.0 .. 3.0).
                    """
                    try:
                        nh = check_net_health()
                        if nh.ok:
                            return 1.0
                        jlog(
                            "network",
                            f"Network slow during {phase}: rtt={nh.rtt_ms}ms http={nh.http_ms}ms ({nh.detail}). Increasing timeouts.",
                            "WARN",
                        )
                        return 2.0
                    except Exception as e:
                        jlog("network", f"Network check failed during {phase}: {e}. Continuing.", "WARN")
                        return 1.5

                # No encryption: password_enc column stores plaintext password.
                password = job.password_enc or ""
                # If user still has legacy encrypted jobs (from old versions), fail fast with clear message.
                if password.startswith("gAAAA"):
                    jlog(
                        "error",
                        "Legacy encrypted password job detected. This version does not support encryption. "
                        "Please create a NEW job (or delete storage/app.db).",
                        "ERROR",
                    )
                    job_repo.mark_error(job_id, "Legacy encrypted password job (recreate job / delete storage/app.db)")
                    continue

                params = RunParams(
                    email=job.email,
                    password=password,
                    keyword=keyword,
                    max_posts=int(job.max_posts),
                    delay_min_sec=float(job.delay_min_sec),
                    delay_max_sec=float(job.delay_max_sec),
                    recognition_enabled=_worker_state_bool(conn, "post_capture_recognition_enabled", True),
                )

                # attempt counter for retryable timeouts
                job = job_repo.bump_attempt(job_id)
                jlog("start", f"Job started (attempt {job.attempt}/{job.max_attempts})")
                progress(0, -1 if params.max_posts <= 0 else params.max_posts)

                try:
                    if shutdown_requested:
                        raise SystemExit(0)
                    if should_cancel():
                        jlog("cancel", "Cancelled before running.", "WARN")
                        job_repo.mark_cancelled(job_id)
                        continue

                    # If browser/page got closed unexpectedly, relaunch before actions.
                    if page is None or (hasattr(page, "is_closed") and page.is_closed()):
                        relaunch()

                    # Adapt timeouts based on network health.
                    # New flow: go directly to filter URL; if redirected to login, handle login and return to URL.
                    mult = net_guard("search")
                    try:
                        page.set_default_timeout(int(30_000 * mult))
                        page.set_default_navigation_timeout(int(60_000 * mult))
                    except Exception:
                        pass

                    # IMPORTANT: always confirm login on facebook.com home first.
                    page = ensure_home_logged_in(page, params, lambda s, m: jlog(s, m))

                    page = search_keyword(page, params, lambda s, m: jlog(s, m))
                    try:
                        last_page_url = str(page.url or "")
                    except Exception:
                        pass
                    if shutdown_requested:
                        raise SystemExit(0)

                    # Filter is applied directly via search URL (see search_keyword()).

                    mult = net_guard("capture")
                    try:
                        # Capture (scroll + expand + element screenshots) can take much longer than 30s on FB
                        # due to lazy rendering, heavy posts, and nested scroll containers.
                        # Increase default action timeout so locator.screenshot doesn't fail early.
                        page.set_default_timeout(int(120_000 * mult))
                        page.set_default_navigation_timeout(int(60_000 * mult))
                    except Exception:
                        pass

                    try:
                        if page is not None and not bool(int(getattr(job, "headless", 0))):
                            page.bring_to_front()
                            time.sleep(0.25)
                    except Exception:
                        pass

                    saved = capture_posts(
                        page,
                        params,
                        posts_root=posts_root,
                        progress=progress,
                        # capture_posts may call log(step, msg, level)
                        log=lambda s, m, lvl="INFO": jlog(s, m, lvl),
                        should_cancel=should_cancel_watchdog,
                        expected_search_url=last_page_url or None,
                    )
                    if saved <= 0 and not should_cancel() and not watchdog_tripped["timeout"]:
                        jlog("capture", "Saved=0 for this keyword. Will still finish job, but check capture logs above.", "WARN")
                    if shutdown_requested:
                        raise SystemExit(0)

                    if should_cancel():
                        jlog("cancel", f"Cancelled. Saved={saved}", "WARN")
                        job_repo.mark_cancelled(job_id)
                    elif watchdog_tripped["timeout"]:
                        # Retry once if attempts remain; otherwise mark error.
                        msg = f"No progress timeout exceeded ({int(no_progress_timeout_s)}s)"
                        if job.attempt < job.max_attempts and not should_cancel():
                            jlog("retry", f"{msg}. Relaunch + retry same keyword.", "WARN")
                            try:
                                relaunch()
                            except Exception:
                                pass
                            job_repo.reset_to_pending_for_retry(job_id, msg, worker_id=worker_id)
                        else:
                            job_repo.mark_error(job_id, msg)
                    else:
                        # For unlimited runs, keep total as -1 ("∞") in UI.
                        progress(saved, -1 if params.max_posts <= 0 else max(saved, params.max_posts))
                        jlog("done", f"Completed. Saved={saved}")
                        job_repo.mark_done(job_id)

                except CaptchaOrCheckpointDetected as e:
                    # NEW: do NOT auto-relaunch/retry. Ask user via UI modal.
                    msg = str(e)
                    jlog("antiblock", msg, "ERROR")
                    try:
                        job_repo.mark_checkpoint_pending(job_id, msg)
                    except Exception:
                        pass

                    # Wait for user decision: "reload" or "continue"
                    decision = None
                    t0 = time.time()
                    while True:
                        if shutdown_requested:
                            raise SystemExit(0)
                        if should_cancel():
                            jlog("cancel", "Cancelled while waiting for checkpoint decision.", "WARN")
                            job_repo.mark_cancelled(job_id)
                            decision = "cancel"
                            break
                        st = job_repo.get_checkpoint_state(job_id)
                        d = str(st.get("decision") or "").strip().lower()
                        if d in {"reload", "continue"}:
                            decision = d
                            break
                        # heartbeat log while waiting
                        if int(time.time() - t0) % 10 == 0:
                            try:
                                jlog("antiblock", "Waiting for UI decision (reload/continue)…", "WARN")
                            except Exception:
                                pass
                        time.sleep(0.5)

                    if decision == "reload":
                        jlog("retry", "User confirmed reload. Relaunch + retry same keyword.", "WARN")
                        try:
                            relaunch()
                        except Exception:
                            pass
                        try:
                            job_repo.clear_checkpoint(job_id)
                        except Exception:
                            pass
                        job_repo.reset_to_pending_for_retry(job_id, msg, worker_id=worker_id)
                        jlog("retry", "Reset job to pending for retry (user-confirmed antiblock).", "WARN")
                    elif decision == "continue":
                        jlog(
                            "antiblock",
                            "User chose continue. Will NOT reload. Waiting for you to finish verification on this Chrome window…",
                            "WARN",
                        )
                        try:
                            job_repo.clear_checkpoint(job_id)
                        except Exception:
                            pass

                        # New behavior: DO NOT attempt capture while we're still on a verification/checkpoint URL.
                        # Wait until the user finishes verification and the tab returns to a normal FB page,
                        # then navigate back to the expected search URL and continue capture.
                        try:
                            t_wait0 = time.time()
                            last_hint = 0.0
                            while True:
                                if shutdown_requested:
                                    raise SystemExit(0)
                                if should_cancel():
                                    jlog("cancel", "Cancelled while waiting for verification.", "WARN")
                                    job_repo.mark_cancelled(job_id)
                                    decision = "cancel"
                                    break
                                try:
                                    cur_url = str(page.url or "")
                                except Exception:
                                    cur_url = ""
                                cur_low = cur_url.lower()
                                in_challenge = any(
                                    x in cur_low
                                    for x in [
                                        "two_step_verification",
                                        "checkpoint",
                                        "authentication",
                                        "/recover",
                                    ]
                                )
                                if not in_challenge:
                                    break
                                now = time.time()
                                if now - last_hint >= 10.0:
                                    last_hint = now
                                    jlog(
                                        "antiblock",
                                        f"Still on verification page. Please complete it in Chrome. (elapsed={int(now-t_wait0)}s)",
                                        "WARN",
                                    )
                                time.sleep(1.0)

                            if decision == "cancel":
                                pass
                            else:
                                # Navigate back to search and continue capture.
                                if last_page_url:
                                    try:
                                        jlog("capture", "Verification finished. Returning to search URL…", "INFO")
                                        page.goto(last_page_url, wait_until="domcontentloaded", timeout=60_000)
                                    except Exception:
                                        pass
                                saved = capture_posts(
                                    page,
                                    params,
                                    posts_root=posts_root,
                                    progress=progress,
                                    log=lambda s, m, lvl="INFO": jlog(s, m, lvl),
                                    should_cancel=should_cancel_watchdog,
                                    expected_search_url=last_page_url or None,
                                )
                                if should_cancel():
                                    jlog("cancel", f"Cancelled. Saved={saved}", "WARN")
                                    job_repo.mark_cancelled(job_id)
                                else:
                                    progress(saved, -1 if params.max_posts <= 0 else max(saved, params.max_posts))
                                    jlog("done", f"Completed. Saved={saved}")
                                    job_repo.mark_done(job_id)
                        except CaptchaOrCheckpointDetected as e2:
                            # If it triggers again, we'll prompt UI again.
                            raise e2
                        except Exception as e2:
                            jlog("error", f"Continue-after-checkpoint failed: {e2}", "ERROR")
                            job_repo.mark_error(job_id, f"Continue-after-checkpoint failed: {e2}")
                    else:
                        job_repo.mark_error(job_id, msg)

                except ElementTimeout as e:
                    # Retry lightly for element timeouts only (1-2 times)
                    jlog("retry", f"Element timeout: {e}", "WARN")
                    if job.attempt < job.max_attempts and not should_cancel():
                        jlog("relaunch", "Relaunching browser before retry.", "WARN")
                        relaunch()
                        job_repo.reset_to_pending_for_retry(job_id, str(e), worker_id=worker_id)
                        jlog("retry", "Reset job to pending for retry.", "WARN")
                    else:
                        job_repo.mark_error(job_id, str(e))

                except PWTimeoutError as e:
                    jlog("retry", f"Playwright timeout: {e}", "WARN")
                    if job.attempt < job.max_attempts and not should_cancel():
                        job_repo.reset_to_pending_for_retry(job_id, f"Playwright timeout: {e}", worker_id=worker_id)
                    else:
                        job_repo.mark_error(job_id, f"Playwright timeout: {e}")

                except PWError as e:
                    # If Chrome/profile/context got closed (user closed window, crash, etc.),
                    if _is_target_closed_error(e):
                        # User requirement: do NOT relaunch/retry when terminal/Chrome closes.
                        # Treat this as a terminal stop / Chrome crash and end the job.
                        jlog("error", f"Browser/page closed unexpectedly. Stop job. ({e})", "ERROR")
                        job_repo.mark_error(job_id, f"Browser/page closed: {e}")
                    else:
                        jlog(
                            "error",
                            f"Playwright error: {e}",
                            "ERROR",
                            data={"trace": traceback.format_exc()},
                        )
                        job_repo.mark_error(job_id, f"Playwright error: {e}")

                except Exception as e:
                    if _is_target_closed_error(e):
                        # User requirement: do NOT relaunch/retry when terminal/Chrome closes.
                        jlog("error", f"Closed target detected. Stop job. ({e})", "ERROR")
                        job_repo.mark_error(job_id, f"Closed target: {e}")
                    elif "login failed" in str(e).lower():
                        # Don't retry login-failed endlessly; often caused by 2FA/captcha.
                        job_repo.mark_error(job_id, str(e))
                    else:
                        jlog("error", f"Unhandled error: {e}", "ERROR", data={"trace": traceback.format_exc()})
                        job_repo.mark_error(job_id, f"Unhandled error: {e}")

                # Per user requirement: no between-keywords delay.
                pass
    finally:
        profile_lock.release()
        lock.release()


if __name__ == "__main__":
    main()

