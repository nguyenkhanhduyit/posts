from __future__ import annotations

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


class _SingleInstanceLock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fh = None

    def acquire(self, content: str) -> bool:
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
            self._fh.write(content)
            self._fh.flush()
            return True
        except Exception:
            try:
                self._fh.seek(0)
                existing = self._fh.read()
            except Exception:
                existing = ""
            if existing.strip():
                print("[supervisor] Another instance is already running.")
                print(existing.strip())
            else:
                print("[supervisor] Another instance is already running.")
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


def _popen(args: list[str], env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )


def _port_is_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _find_free_port(host: str, preferred: int, span: int = 20) -> int | None:
    for p in range(preferred, preferred + span):
        if _port_is_free(host, p):
            return p
    return None


def main() -> int:
    env = os.environ.copy()
    host = env.get("BACKEND_HOST", "127.0.0.1")
    preferred_port = int(env.get("BACKEND_PORT", "8080"))
    # Initial worker count (can be overridden at runtime via UI settings stored in SQLite worker_state)
    worker_count = max(1, int(env.get("WORKER_COUNT", "1")))
    shutdown_requested = False

    def request_shutdown(reason: str) -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        try:
            print(f"\n[supervisor] Shutdown requested: {reason}\n")
        except Exception:
            pass

    # Windows: make sure closing the terminal window stops ALL children too.
    # (KeyboardInterrupt is not always raised on terminal-close.)
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

    def _read_worker_count_from_db() -> int | None:
        try:
            from app.backend.config import load_settings
            from app.backend.db.sqlite import connect, migrate

            s = load_settings()
            c = connect(s.sqlite_path)
            migrate(c)
            row = c.execute("SELECT value FROM worker_state WHERE key='worker_count';").fetchone()
            try:
                c.close()
            except Exception:
                pass
            if not row:
                return None
            n = int(str(row["value"]).strip())
            return max(1, min(8, n))
        except Exception:
            return None

    def _kill_stale_processes_on_start() -> None:
        """
        User requirement: starting a NEW terminal session must be a clean slate.
        If the previous terminal was closed forcefully, backend/workers/Chrome may remain as orphans.
        On Windows we can safely kill ONLY processes that clearly belong to this tool.
        """
        mode = (os.getenv("CLEAN_PROCESSES_ON_START", "1") or "1").strip().lower()
        if mode in {"0", "false", "no", "off"}:
            return
        if os.name != "nt":
            return
        # 1) Strong cleanup: kill PIDs recorded in our lock files (worker + supervisor).
        # This catches orphaned python processes even if commandline filtering fails.
        killed: set[int] = set()

        def _parse_pid(lock_text: str) -> int | None:
            for ln in (lock_text or "").splitlines():
                s = (ln or "").strip()
                if s.lower().startswith("pid="):
                    try:
                        return int(s.split("pid=", 1)[1].strip())
                    except Exception:
                        return None
            return None

        def _taskkill(pid: int) -> bool:
            if pid <= 0:
                return False
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                return False

        try:
            storage = (Path("app") / "storage").resolve()
            if storage.exists():
                for p in sorted(storage.glob("*.lock")):
                    try:
                        txt = p.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        txt = ""
                    pid = _parse_pid(txt)
                    if pid is None or pid in killed:
                        continue
                    if _taskkill(pid):
                        killed.add(pid)
        except Exception:
            pass

        try:
            if killed:
                print(f"[supervisor] Cleaned stale lock PIDs on start: killed {len(killed)} process tree(s).")
        except Exception:
            pass

        # 2) Best-effort cleanup by commandline (chrome profile), in case lock files are missing/stale.
        try:
            from app.backend.config import load_settings

            s = load_settings()
            profile_dir = str(s.chrome_profile_dir)
        except Exception:
            profile_dir = ""

        # Kill stale python processes of THIS repo (backend + worker).
        # Kill stale chrome.exe instances that use our user-data-dir under app/chrome-profile*.
        # Use CIM to read command lines (Taskkill cannot filter by args).
        try:
            pp = str(Path(profile_dir).parent) if profile_dir else ""
            ps_cmd = "\n".join(
                [
                    "$ErrorActionPreference='SilentlyContinue'",
                    "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -ne $null }",
                    "$targets = @()",
                    "$targets += $procs | Where-Object {",
                    "  ($_.Name -like 'python*.exe' -or $_.Name -like 'py.exe') -and",
                    "  ($_.CommandLine -match 'app\\.backend\\.app' -or $_.CommandLine -match 'app\\.worker\\.runner')",
                    "}",
                    f"$pp = '{pp}'",
                    "$targets += $procs | Where-Object {",
                    "  $_.Name -eq 'chrome.exe' -and",
                    "  ($_.CommandLine -match '--user-data-dir=') -and",
                    "  ($pp -ne '' -and $_.CommandLine -like ('*--user-data-dir=' + $pp + '\\\\chrome-profile*'))",
                    "}",
                    "if ($targets.Count -gt 0) {",
                    "  $pids = ($targets | Select-Object -ExpandProperty ProcessId | Sort-Object -Unique)",
                    "  foreach ($pid in $pids) {",
                    "    try { Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue } catch {}",
                    "  }",
                    "  Write-Host ('[supervisor] Cleaned stale processes on start: killed ' + $pids.Count + ' process(es).')",
                    "} else {",
                    "  Write-Host '[supervisor] No stale processes found on start (cmdline scan).'",
                    "}",
                ]
            )
            ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd]
            subprocess.run(ps, check=False, stdout=sys.stdout, stderr=sys.stderr)
        except Exception as e:
            try:
                print(f"[supervisor] Clean stale processes on start failed (ignored): {e}")
            except Exception:
                pass

    def _reset_queue_on_start() -> None:
        """
        User requirement: when starting a new terminal/session, do NOT run leftover jobs.
        We mark any pending/running jobs as cancelled, and keep history of finished/error ones.
        """
        mode = (os.getenv("RESET_QUEUE_ON_START", "1") or "1").strip().lower()
        if mode in {"0", "false", "no", "off"}:
            return
        try:
            from app.backend.config import load_settings
            from app.backend.db.sqlite import connect, migrate
            from app.utils.timeutil import utc_now_iso

            s = load_settings()
            c = connect(s.sqlite_path)
            migrate(c)
            now = utc_now_iso()
            with c:
                c.execute(
                    """
                    UPDATE jobs
                    SET status='cancelled',
                        finished_at=?,
                        last_error='Reset on start (new terminal session)',
                        retry_worker_id=NULL
                    WHERE status IN ('pending','running');
                    """,
                    (now,),
                )
            try:
                c.close()
            except Exception:
                pass
            try:
                print("[supervisor] Reset queue on start: cancelled leftover pending/running jobs.")
            except Exception:
                pass
        except Exception as e:
            try:
                print(f"[supervisor] Reset queue on start failed (ignored): {e}")
            except Exception:
                pass

    # Prevent multiple run.bat instances from spawning multiple backends/workers.
    lock = _SingleInstanceLock(Path("app") / "storage" / "supervisor.lock")
    if not lock.acquire(f"pid={os.getpid()}\n(note) Close the other run.bat window.\n"):
        return 3

    # Clean any stale processes BEFORE resetting queue / starting new backend/workers.
    _kill_stale_processes_on_start()

    # Reset leftover jobs before starting backend/workers.
    _reset_queue_on_start()

    port = _find_free_port(host, preferred_port, span=30)
    if port is None:
        print(f"[supervisor] No free port found in range {preferred_port}..{preferred_port+29}")
        lock.release()
        return 2

    if port != preferred_port:
        print(f"[supervisor] Port {preferred_port} is busy. Using {host}:{port} instead.")

    child_env = env.copy()
    child_env["BACKEND_HOST"] = host
    child_env["BACKEND_PORT"] = str(port)

    backend = _popen([sys.executable, "-m", "app.backend.app"], env=child_env)
    time.sleep(1.2)
    workers: list[subprocess.Popen] = []

    ui_url = f"http://{host}:{port}"

    # Wait briefly for backend health before opening browser.
    health_url = f"{ui_url}/health"
    for _ in range(25):
        try:
            with urllib.request.urlopen(health_url, timeout=1.2) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.25)

    # Sync worker count from DB before deciding whether to auto-open a browser tab.
    desired = _read_worker_count_from_db()
    if desired is not None:
        worker_count = desired

    open_ui = os.getenv("OPEN_UI_BROWSER", "1").strip().lower() not in {"0", "false", "no"}
    # User requirement: always open UI when enabled, even if worker_count>=2.
    # This may open an extra browser window (often Chrome). Disable with OPEN_UI_BROWSER=0 if undesired.
    if open_ui:
        try:
            webbrowser.open(ui_url, new=1, autoraise=True)
        except Exception:
            pass
    elif open_ui:
        pass

    print(f"\nOpen UI: {ui_url}\nPress Ctrl+C to stop all.\n")
    # Update lock info with actual URL
    try:
        lock._fh.seek(0)
        lock._fh.truncate(0)
        lock._fh.write(f"pid={os.getpid()}\nurl={ui_url}\n")
        lock._fh.flush()
    except Exception:
        pass

    try:
        while True:
            if shutdown_requested:
                break
            # Ensure all workers are running.
            if backend.poll() is None:
                # Hot-reload desired worker count from DB (UI input).
                desired = _read_worker_count_from_db()
                if desired is not None and desired != worker_count:
                    print(f"[supervisor] Updating workers: {worker_count} -> {desired}")
                    worker_count = desired

                # If decreased, stop extra workers gracefully.
                while len(workers) > worker_count:
                    w = workers.pop()
                    try:
                        if w.poll() is None:
                            if os.name == "nt":
                                w.send_signal(signal.CTRL_BREAK_EVENT)
                            else:
                                w.terminate()
                    except Exception:
                        pass
                    time.sleep(0.2)

                # Spawn missing workers.
                if len(workers) < worker_count:
                    for wid in range(len(workers), worker_count):
                        wenv = child_env.copy()
                        wenv["WORKER_ID"] = str(wid)
                        workers.append(_popen([sys.executable, "-m", "app.worker.runner"], env=wenv))
                        time.sleep(0.2)

                # Restart workers that exited.
                for i, w in enumerate(list(workers)):
                    if w.poll() is None:
                        continue
                    code = w.returncode
                    if code == 3:
                        print(f"[supervisor] Worker w{i} already running elsewhere. Will retry in 5s...")
                        time.sleep(5.0)
                    elif code is not None and code != 0:
                        print(f"[supervisor] Worker w{i} exited with code {code}. Restarting in 3s...")
                        time.sleep(3.0)
                    wenv = child_env.copy()
                    wenv["WORKER_ID"] = str(i)
                    workers[i] = _popen([sys.executable, "-m", "app.worker.runner"], env=wenv)

            if backend.poll() is not None:
                print("\n[supervisor] Backend exited. Stopping worker...")
                break
            time.sleep(0.7)
    except KeyboardInterrupt:
        request_shutdown("KeyboardInterrupt (Ctrl+C)")

    for p in [*workers, backend]:
        if p is None:
            continue
        if p.poll() is None:
            try:
                if os.name == "nt":
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    p.terminate()
            except Exception:
                pass

    # Give children a moment to exit gracefully.
    time.sleep(1.2)
    for p in [*workers, backend]:
        if p is None:
            continue
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
    lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

