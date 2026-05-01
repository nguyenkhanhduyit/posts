from __future__ import annotations

import socket
import time
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class NetHealth:
    ok: bool
    rtt_ms: int
    http_ms: int
    detail: str


def _tcp_rtt_ms(host: str, port: int, timeout_sec: float) -> int:
    t0 = time.perf_counter()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout_sec)
        s.connect((host, port))
        return int((time.perf_counter() - t0) * 1000)
    finally:
        try:
            s.close()
        except Exception:
            pass


def _http_get_ms(url: str, timeout_sec: float) -> int:
    t0 = time.perf_counter()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        _ = resp.read(32)
    return int((time.perf_counter() - t0) * 1000)


def check_net_health(
    *,
    tcp_host: str = "1.1.1.1",
    tcp_port: int = 443,
    tcp_timeout_sec: float = 2.5,
    http_url: str = "https://www.facebook.com/robots.txt",
    http_timeout_sec: float = 4.5,
    warn_rtt_ms: int = 450,
    warn_http_ms: int = 2200,
) -> NetHealth:
    """
    Best-effort network health check:
    - TCP connect RTT to a stable endpoint (Cloudflare 1.1.1.1:443)
    - Small HTTP GET (Facebook robots.txt) to reflect real target reachability
    """
    rtt_ms = -1
    http_ms = -1
    try:
        rtt_ms = _tcp_rtt_ms(tcp_host, tcp_port, tcp_timeout_sec)
    except Exception as e:
        return NetHealth(ok=False, rtt_ms=-1, http_ms=-1, detail=f"tcp_connect_failed: {e}")

    try:
        http_ms = _http_get_ms(http_url, http_timeout_sec)
    except Exception as e:
        # Internet works but FB might be slow/blocked; still treat as unhealthy for this tool.
        return NetHealth(ok=False, rtt_ms=rtt_ms, http_ms=-1, detail=f"http_failed: {e}")

    ok = rtt_ms <= warn_rtt_ms and http_ms <= warn_http_ms
    detail = "ok" if ok else "slow"
    return NetHealth(ok=ok, rtt_ms=rtt_ms, http_ms=http_ms, detail=detail)

