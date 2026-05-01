from __future__ import annotations

import os
import random
import re
import time
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import Error, Page, TimeoutError as PWTimeoutError
from urllib.parse import urlparse, parse_qs

from app.utils.sanitize import sanitize_keyword_for_path
from app.utils.timeutil import local_date_yyyy_mm_dd
from app.utils.imagehash import dhash64_int_bytes, hamming_distance_u64

from app.worker.fb_automation.selectors import (
    FEED_FALLBACK_CLASS,
    FEED_ROLE,
    FILTER_CONTAINER_CLASS,
    FILTER_CONTAINER_CLASS_ALT,
    FILTER_TRIGGER_CLASS,
    FILTER_RECENT_ARIA_LABEL,
    POST_ARIA_POSINSET_SELECTOR,
    POST_ARTICLE_SELECTOR,
    POST_FOCUS_SELECTOR,
    SEARCH_INPUT_FALLBACK_CLASS,
    SEE_MORE_BUTTON_CLASS,
)


class CaptchaOrCheckpointDetected(RuntimeError):
    pass


class ElementTimeout(RuntimeError):
    pass


@dataclass(frozen=True)
class RunParams:
    email: str
    password: str
    keyword: str
    max_posts: int
    delay_min_sec: float
    delay_max_sec: float


RECENT_POSTS_FILTERS = (
    "eyJyZWNlbnRfcG9zdHM6MCI6IntcIm5hbWVcIjpcInJlY2VudF9wb3N0c1wiLFwiYXJnc1wiOlwiXCJ9In0%3D"
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if raw is None:
        return default
    s = str(raw).strip().lower()
    if s == "":
        return default
    return s in {"1", "true", "yes", "on", "y"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if raw is None:
        return default
    s = str(raw).strip()
    if s == "":
        return default
    try:
        return float(s)
    except Exception:
        return default


def _rand(a: float, b: float) -> float:
    return random.uniform(a, b)


def _sleep_action(
    params: RunParams,
    log: Optional[Callable[[str, str], None]] = None,
    reason: str = "cooldown",
) -> float:
    """
    Anti-block sleep.
    Returns the sleep duration (seconds) so callers can log or branch.
    """
    wait_s = float(_rand(params.delay_min_sec, params.delay_max_sec))
    # Avoid spamming logs for tiny sleeps
    if log is not None and wait_s >= 0.7:
        try:
            log("cooldown", f"Cooldown {wait_s:.1f}s ({reason})")
        except Exception:
            pass
    time.sleep(wait_s)
    return wait_s


def _typing_delay_ms() -> int:
    return int(_rand(50, 150))


def _move_and_click(page: Page, locator) -> None:
    # Avoid bounding_box() because it can hang when element is offscreen/covered.
    try:
        locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        locator.hover(timeout=5000)
        time.sleep(_rand(0.12, 0.35))
    except Exception:
        pass
    locator.click(timeout=15_000)


def _detect_checkpoint(page: Page) -> None:
    url = (page.url or "").lower()
    # URL-based signals (checkpoint, two-step verification, auth challenges)
    if any(x in url for x in ["checkpoint", "two_step_verification", "authentication"]):
        raise CaptchaOrCheckpointDetected(f"Security challenge detected by URL: {page.url}")
    try:
        # Avoid scanning full body text (can produce false positives on FB search pages).
        evidence = page.evaluate(
            """() => {
              try {
                const url = location.href || '';
                const title = document.title || '';
                const hasRecaptcha = !!document.querySelector('iframe[src*="recaptcha"], iframe[title*="recaptcha" i], div.g-recaptcha');
                const hasCaptchaInput = !!document.querySelector('input[name*="captcha" i], input[id*="captcha" i], input[aria-label*="captcha" i]');
                const hasCheckpointForm = !!document.querySelector('form[action*="checkpoint"], input[name="approvals_code"], input[name="code"]');
                const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).slice(0, 20);
                const norm = (s) => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();
                const hit = (s) => {
                  const t = norm(s);
                  if (!t) return '';
                  const keys = ['captcha', \"i'm not a robot\", 'recaptcha', 'xác minh', 'verify', 'security check', 'checkpoint'];
                  for (const k of keys) if (t.includes(k)) return k;
                  return '';
                };
                let dialogHit = '';
                let dialogSnippet = '';
                for (const d of dialogs) {
                  const k = hit(d.innerText || d.textContent || '');
                  if (k) { dialogHit = k; dialogSnippet = norm(d.innerText || d.textContent || '').slice(0, 220); break; }
                }
                return { url, title, hasRecaptcha, hasCaptchaInput, hasCheckpointForm, dialogHit, dialogSnippet };
              } catch (e) {
                return { url: '', title: '', hasRecaptcha: false, hasCaptchaInput: false, hasCheckpointForm: false, dialogHit: '', dialogSnippet: '' };
              }
            }"""
        ) or {}

        strong = bool(evidence.get("hasRecaptcha")) or bool(evidence.get("hasCaptchaInput")) or bool(evidence.get("hasCheckpointForm")) or bool(evidence.get("dialogHit"))
        if strong:
            msg = f"Checkpoint/Captcha detected. url={evidence.get('url') or page.url} title={evidence.get('title') or ''}"
            if evidence.get("dialogHit"):
                msg += f" dialogHit={evidence.get('dialogHit')} dialogSnippet={evidence.get('dialogSnippet')}"
            raise CaptchaOrCheckpointDetected(msg)
    except PWTimeoutError:
        return


def _ensure_open_page(page: Page) -> Page:
    try:
        if page is not None and hasattr(page, "is_closed") and page.is_closed():
            try:
                return page.context.new_page()
            except Exception:
                return page
    except Exception:
        return page
    return page


def _goto_home(page: Page) -> Page:
    """
    Defensive navigation helper.
    Persistent profiles on Windows can intermittently close the current tab during navigation.
    When that happens, re-open a new page in the same context and retry once.
    """
    page = _ensure_open_page(page)
    try:
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
        return page
    except Exception as e:
        msg = str(e).lower()
        if "has been closed" in msg or "target page" in msg or "browser has been closed" in msg or "connection closed" in msg:
            page = page.context.new_page()
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
            return page
        raise

def _goto_login(page: Page) -> Page:
    # Avoid extra hop to facebook.com home. If already logged in, FB often redirects away.
    page = _ensure_open_page(page)
    try:
        page.goto("https://www.facebook.com/login.php", wait_until="domcontentloaded", timeout=60_000)
        return page
    except Exception as e:
        msg = str(e).lower()
        if "has been closed" in msg or "target page" in msg or "browser has been closed" in msg or "connection closed" in msg:
            page = page.context.new_page()
            page.goto("https://www.facebook.com/login.php", wait_until="domcontentloaded", timeout=60_000)
            return page
        raise

def _try_dismiss_cookie_consent(page: Page) -> None:
    """
    Best-effort: cookie/consent banners can cover the login form, making inputs "not visible".
    We only click obvious consent buttons by role/text.
    """
    labels = [
        "Allow all cookies",
        "Accept all",
        "Accept All",
        "Allow essential and optional cookies",
        "Only allow essential cookies",
        "Cho phép tất cả cookie",
        "Chấp nhận tất cả",
        "Chỉ cho phép cookie cần thiết",
        "Đồng ý",
        "OK",
    ]
    for name in labels:
        try:
            btn = page.get_by_role("button", name=name).first
            if btn.count() > 0 and btn.is_visible(timeout=800):
                btn.click(timeout=2500)
                time.sleep(_rand(0.2, 0.5))
                return
        except Exception:
            continue


def _find_login_inputs(page: Page):
    """
    FB login fields are not always stable across locales/layouts.
    Return (email_locator, pass_locator) or (None, None).
    """
    # Prefer attributes that FB keeps consistent.
    email_candidates = [
        'input[name="email"]',
        'input#email',
        'input[autocomplete="username"]',
        'input[type="text"][name]',
        'input[type="text"]',
        'input[type="email"]',
        '[role="textbox"][name*="Email"]',
    ]
    pass_candidates = [
        'input[name="pass"]',
        'input#pass',
        'input[type="password"]',
        'input[autocomplete="current-password"]',
    ]
    email = None
    pw = None
    for sel in email_candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                email = loc
                break
        except Exception:
            continue
    for sel in pass_candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                pw = loc
                break
        except Exception:
            continue
    return email, pw


def _is_logged_in(page: Page) -> bool:
    # Heuristic: presence of searchbox and absence of login fields.
    if page.locator('input[name="email"]').count() > 0:
        return False
    if page.locator('input[name="pass"]').count() > 0:
        return False
    return page.locator('[role="search"], [role="searchbox"], input[aria-label*="Tìm kiếm"]').count() > 0


def ensure_login(page: Page, params: RunParams, log: Callable[[str, str], None]) -> Page:
    # Prefer to login on the CURRENT page to avoid extra navigations that can crash/close the driver.
    # Only navigate to /login.php if we are not already on a login-like page.
    try:
        cur = (page.url or "").lower()
    except Exception:
        cur = ""
    try:
        has_login_inputs = page.locator('input[name="email"], input#email, input[name="pass"], input#pass, input[type="password"]').count() > 0
    except Exception:
        has_login_inputs = False
    if not ("login" in cur or has_login_inputs):
        page = _goto_login(page)
    _detect_checkpoint(page)

    if _is_logged_in(page):
        log("login", "Already logged in (profile session).")
        return page

    log("login", "Logging in...")
    try:
        # Consent overlays can block visibility of the login form.
        _try_dismiss_cookie_consent(page)

        email_inp, pass_inp = _find_login_inputs(page)
        if email_inp is None or pass_inp is None:
            # Try one more time after a short wait + another consent attempt.
            try:
                page.wait_for_timeout(600)
            except Exception:
                time.sleep(0.6)
            _try_dismiss_cookie_consent(page)
            email_inp, pass_inp = _find_login_inputs(page)

        if email_inp is None:
            raise ElementTimeout('Login input not found: email')
        if pass_inp is None:
            raise ElementTimeout('Login input not found: password')

        # Wait longer; network / FB can be slow.
        email_inp.wait_for(timeout=60_000)
        _move_and_click(page, email_inp)
        email_inp.fill("")
        for ch in params.email:
            email_inp.type(ch, delay=_typing_delay_ms())
        time.sleep(_rand(0.2, 0.5))
        _move_and_click(page, pass_inp)
        pass_inp.fill("")
        for ch in params.password:
            pass_inp.type(ch, delay=_typing_delay_ms())
        time.sleep(_rand(0.2, 0.5))

        # Primary: press Enter to submit (more resilient than chasing button selectors).
        try:
            pass_inp.press("Enter", timeout=5000)
        except Exception:
            pass

        # Fallback: click common login buttons (role/name stable-ish).
        candidates = [
            'button[name="login"]',
            '[data-testid="royal_login_button"]',
            'button[type="submit"]',
        ]
        clicked = False
        for sel in candidates:
            loc = page.locator(sel).first
            if loc.count() > 0:
                try:
                    _move_and_click(page, loc)
                    clicked = True
                    break
                except Exception:
                    continue

        if not clicked:
            # Role-based fallback (works across locale)
            for name in ["Đăng nhập", "Log in", "Login"]:
                try:
                    btn = page.get_by_role("button", name=name).first
                    if btn.count() > 0:
                        _move_and_click(page, btn)
                        clicked = True
                        break
                except Exception:
                    continue

        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        _detect_checkpoint(page)
    except PWTimeoutError as e:
        raise ElementTimeout(f"Login element timeout: {e}")
    except ElementTimeout:
        # Add cheap diagnostics to logs (helps when FB changes layout or blocks the page).
        try:
            info = page.evaluate(
                """() => {
                  const t = document.title || '';
                  const inputs = document.querySelectorAll('input').length;
                  const pw = document.querySelectorAll('input[type="password"]').length;
                  const forms = document.querySelectorAll('form').length;
                  const bodyTxt = (document.body && (document.body.innerText||'')) ? (document.body.innerText||'').slice(0, 180) : '';
                  return { title: t, inputs, pw, forms, bodyHead: bodyTxt };
                }"""
            )
        except Exception:
            info = None
        try:
            log("login", f"Login diagnostics: url={page.url} info={info}")
        except Exception:
            pass
        raise

    if not _is_logged_in(page):
        # If we are challenged, raise a hard-stop error (no retry).
        _detect_checkpoint(page)
        raise RuntimeError("Login failed (not logged in after submit).")

    log("login", "Login OK.")
    return page


def ensure_home_logged_in(page: Page, params: RunParams, log: Callable[[str, str], None]) -> Page:
    """
    Per user requirement: always visit https://www.facebook.com/ first to confirm login state.
    If not logged in, perform login, then return to home.
    """
    page = _goto_home(page)
    _detect_checkpoint(page)
    if _is_logged_in(page):
        log("login", "Already logged in (home).")
        return page
    log("login", "Not logged in at home. Logging in…")
    page = ensure_login(page, params, log)
    # Re-check at home (some login flows redirect elsewhere)
    try:
        page = _goto_home(page)
    except Exception:
        pass
    _detect_checkpoint(page)
    return page


def _find_search_input(page: Page):
    # Prefer stable aria-label/role selectors first.
    candidates = [
        'input[aria-label="Tìm kiếm trên Facebook"]',
        'input[aria-label^="Tìm kiếm"]',
        '[role="searchbox"]',
        '[role="search"] input',
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        if loc.count() > 0:
            return loc

    # Fallback: exact class provided by user.
    cls = SEARCH_INPUT_FALLBACK_CLASS
    loc = page.locator(f'input[class="{cls}"], div[class="{cls}"] input').first
    if loc.count() > 0:
        return loc
    return None


def search_keyword(page: Page, params: RunParams, log: Callable[[str, str], None]) -> Page:
    log("search", f'Searching keyword: "{params.keyword}"')
    _detect_checkpoint(page)
    # No pre-search cooldown (start immediately). Cooldown is applied after capture instead.

    # User preference: navigate directly to search URL with recent-posts filters.
    # This avoids fragile home-page searchbox variations and avoids UI toggles.
    try:
        from urllib.parse import quote

        q = quote(params.keyword, safe="")
        url_top = f"https://www.facebook.com/search/top/?q={q}&filters={RECENT_POSTS_FILTERS}"
        url_posts = f"https://www.facebook.com/search/posts/?q={q}&filters={RECENT_POSTS_FILTERS}"

        # Prefer /search/posts in automation: /search/top with filters often returns 404 in headless/automation contexts.
        prefer = (os.getenv("FB_SEARCH_PREFER", "posts") or "posts").strip().lower()
        primary, secondary = (url_posts, url_top) if prefer in {"posts", "post"} else (url_top, url_posts)

        def _goto_with_fallback(u1: str, u2: str) -> str:
            nonlocal page
            page = _ensure_open_page(page)
            resp = page.goto(u1, wait_until="domcontentloaded", timeout=60_000)
            try:
                status = int(resp.status) if resp is not None else 0
            except Exception:
                status = 0
            if status >= 400:
                log("search", f"Search URL returned HTTP {status}. Falling back…", "WARN")
                page = _ensure_open_page(page)
                page.goto(u2, wait_until="domcontentloaded", timeout=60_000)
                return u2
            try:
                # FB sometimes returns a 404 page (soft or hard) for /search/top with filters.
                not_found = bool(
                    page.evaluate(
                        """() => {
                          const t = (document.title || '').toLowerCase();
                          const b = (document.body && (document.body.innerText||'')) ? (document.body.innerText||'').toLowerCase() : '';
                          if (t.includes('page not found') || t.includes('not found')) return true;
                          if (b.includes('page not found') || b.includes('sorry') && b.includes('available')) return true;
                          // VN locale
                          if (b.includes('không tìm thấy') || b.includes('trang bạn yêu cầu')) return true;
                          return false;
                        }"""
                    )
                )
            except Exception:
                not_found = False
            if not_found:
                log("search", f"/search/top returned Not Found. Falling back to /search/posts …", "WARN")
                page = _ensure_open_page(page)
                page.goto(u2, wait_until="domcontentloaded", timeout=60_000)
                return u2
            return u1

        url = _goto_with_fallback(primary, secondary)
        _detect_checkpoint(page)

        # If not logged in, FB will often redirect to a login page or show login inputs.
        # In that case: login, then return to the filter URL.
        try:
            cur = (page.url or "").lower()
        except Exception:
            cur = ""
        login_like = ("login" in cur) or (page.locator('input[name="email"], input#email').count() > 0)
        if login_like and not _is_logged_in(page):
            log("login", "Redirected to login. Logging in, then returning to filter URL…")
            page = ensure_login(page, params, log)
            # Re-open the search URL after login (with the same fallback logic).
            url = _goto_with_fallback(primary, secondary)
            _detect_checkpoint(page)
        return page
    except Exception:
        # Fallback: use UI searchbox if direct navigation fails.
        pass

    # Fallback navigation: avoid home hop; login page is more stable across locales/layouts.
    page = _goto_login(page)
    _detect_checkpoint(page)
    # No pre-search cooldown (fallback).

    inp = _find_search_input(page)
    if inp is None:
        raise ElementTimeout("Search input not found (stable + fallback selectors failed).")

    try:
        inp.wait_for(timeout=20_000)
        _move_and_click(page, inp)
        inp.fill("")
        for ch in params.keyword:
            inp.type(ch, delay=_typing_delay_ms())
        time.sleep(_rand(0.2, 0.6))
        inp.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        _detect_checkpoint(page)
    except PWTimeoutError as e:
        raise ElementTimeout(f"Search typing/enter timeout: {e}")
    return page


def enable_recent_posts_filter(page: Page, params: RunParams, log: Callable[[str, str], None]) -> None:
    # Filters are already applied in search_keyword() via URL.
    log("filter", 'Enabling filter: "Bài viết mới đây"')
    try:
        # Best-effort verify we are on the filtered search page; if not, navigate.
        cur = ""
        try:
            cur = page.url or ""
        except Exception:
            cur = ""
        if "filters=" not in cur or "/search/" not in cur:
            from urllib.parse import quote

            q = quote(params.keyword, safe="")
            url = f"https://www.facebook.com/search/top/?q={q}&filters={RECENT_POSTS_FILTERS}"
            log("filter", "Áp dụng lọc bằng link (search/top + filters)…")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            _detect_checkpoint(page)
        log("filter", 'Filter "Bài viết mới đây" enabled.')
        return
    except Exception as e:
        raise ElementTimeout(f"Filter by URL failed: {e}")


def _find_feed(page: Page):
    loc = page.locator(f'[role="{FEED_ROLE}"]').first
    if loc.count() > 0:
        return loc
    return page.locator(f'div[class="{FEED_FALLBACK_CLASS}"]').first


def _expand_see_more(post, page: Page) -> None:
    """
    Expand long post content before screenshot.
    Priority:
    1) Stable text/role-based: button-like element with text "Xem thêm" (VN) / "See more"
    2) Fallback to the exact class provided by user.

    Important: avoid clicking other interactive zones (reactions, author info).
    """
    # IMPORTANT:
    # Facebook often renders "Xem thêm" as a div[role=button][tabindex=0] with a very stable class
    # (SEE_MORE_BUTTON_CLASS), while the visible text can be in descendants.
    # Strategy:
    # 1) Prefer exact class match (most reliable for this account/layout).
    # 2) Fallback to role=button by text within message scope.

    def _click_all_see_more_in_post() -> int:
        """
        STRICT (per user requirement): a post can contain 1-2 (or more) "see more" buttons.
        We must click ALL of them before screenshot. Click is done via DOM to avoid mis-clicking images/links.
        Returns count clicked (best-effort).
        """
        try:
            return int(
                post.evaluate(
                    """(root, exactCls) => {
                      // NOTE: run in the context of the post element itself.
                      // Using post.evaluate ensures `root` is a real DOM element (Locator -> ElementHandle issues).
                      if (!root) return 0;
                      const normalize = (s) => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();
                      const isSeeMoreText = (t) => {
                        const x = normalize(t);
                        return x === 'xem thêm' || x === 'see more' || x.startsWith('xem thêm') || x.startsWith('see more');
                      };
                      const normCls = (s) => (s || '').trim().replace(/\\s+/g,' ');
                      const isVisibleEnough = (el) => {
                        try {
                          const st = getComputedStyle(el);
                          if (st.display === 'none' || st.visibility === 'hidden') return false;
                          const op = parseFloat(st.opacity || '1');
                          if (op < 0.06) return false;
                          if ((st.pointerEvents || '').toLowerCase() === 'none') return false;
                          const r = el.getBoundingClientRect();
                          if (!r || r.width < 18 || r.height < 10) return false;
                          return true;
                        } catch (e) { return false; }
                      };
                      // Prefer scoping to message body to avoid footer/UFI buttons.
                      const msg = root.querySelector('div[data-ad-preview="message"]');
                      const scope = msg || root;
                      const wantClass = normCls(exactCls || '');

                      // 1) Preferred: exact class match inside scope.
                      const exact = wantClass
                        ? Array.from(scope.querySelectorAll('div[role="button"][tabindex="0"],div[role="button"]'))
                            .filter(el => {
                              try {
                                const c = normCls(el.getAttribute('class'));
                                if (c !== wantClass) return false;
                                const t = normalize(el.innerText || el.textContent || '');
                                return isSeeMoreText(t);
                              } catch (e) { return false; }
                            })
                            .slice(0, 25)
                        : [];

                      // 2) Fallback: find any node containing "xem thêm/see more" then walk up to a clickable ancestor.
                      const findClickable = (node) => {
                        try {
                          let cur = node;
                          for (let i = 0; i < 8 && cur; i++) {
                            const tag = (cur.tagName || '').toLowerCase();
                            const role = (cur.getAttribute && cur.getAttribute('role')) || '';
                            if (tag === 'button' || tag === 'a') return cur;
                            if (role === 'button' || role === 'menuitem') return cur;
                            if (cur.getAttribute && cur.getAttribute('tabindex') === '0') return cur;
                            cur = cur.parentElement;
                          }
                        } catch (e) {}
                        return null;
                      };

                      const nodesWithText = Array.from(scope.querySelectorAll('div,span,a,button'))
                        .filter(n => {
                          try { return isSeeMoreText(n.innerText || n.textContent || ''); } catch (e) { return false; }
                        })
                        .slice(0, 220);

                      const byText = [];
                      for (const n of nodesWithText) {
                        const c = findClickable(n);
                        if (c) byText.push(c);
                      }

                      // Dedup while preserving order
                      const seen = new Set();
                      const uniq = [];
                      for (const el of (exact.length ? exact : byText)) {
                        try {
                          if (!el || !el.isConnected) continue;
                          const k = el;
                          if (seen.has(k)) continue;
                          seen.add(k);
                          uniq.push(el);
                          if (uniq.length >= 60) break;
                        } catch (e) {}
                      }

                      let clicked = 0;
                      for (const el of uniq) {
                        try {
                          if (!el || !el.isConnected) continue;

                          const txt = (el.innerText || el.textContent || '');
                          if (!isSeeMoreText(txt)) continue;
                          const target = el;
                          if (!isVisibleEnough(target)) continue;

                          // Guardrails: avoid clicking outside the post message area (e.g., reactions/comments footer).
                          try {
                            const aria = normalize(target.getAttribute('aria-label') || '');
                            const ttxt = normalize(target.innerText || target.textContent || '');
                            const href = normalize(target.getAttribute('href') || '');
                            const bad =
                              aria.includes('cảm xúc') ||
                              aria.includes('thả cảm xúc') ||
                              aria.includes('reaction') ||
                              aria.includes('reactions') ||
                              aria.includes('tham gia nhóm') ||
                              aria.includes('join group') ||
                              ttxt.includes('tham gia nhóm') ||
                              ttxt.includes('join group') ||
                              ttxt.includes('cảm xúc') ||
                              ttxt.includes('reactions') ||
                              href.includes('ufi/reaction') ||
                              href.includes('reaction');
                            if (bad) continue;
                            // If a message container exists, only click inside it.
                            if (msg && !(msg.contains(target))) continue;
                            // Otherwise, avoid the bottom half of the post (usually footer / UFI area).
                            if (!msg) {
                              const rr = root.getBoundingClientRect();
                              const tr = target.getBoundingClientRect();
                              if (rr && tr && rr.height > 200) {
                                const midY = rr.top + rr.height * 0.55;
                                const tY = tr.top + tr.height * 0.5;
                                if (tY > midY) continue;
                              }
                            }
                          } catch (e) {}

                          try { target.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
                          // Strict: only use element.click() (do NOT dispatch custom MouseEvents).
                          try { target.click(); clicked++; } catch (e) {}
                        } catch (e) {}
                      }
                      return clicked;
                    }""",
                    SEE_MORE_BUTTON_CLASS,
                )
            )
        except Exception:
            return 0

    def _count_see_more_left() -> int:
        try:
            return int(
                post.evaluate(
                    """(root, exactCls) => {
                      // NOTE: run in the context of the post element itself.
                      if (!root) return 0;
                      const normalize = (s) => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();
                      const isSeeMoreText = (t) => {
                        const x = normalize(t);
                        return x === 'xem thêm' || x === 'see more' || x.startsWith('xem thêm') || x.startsWith('see more');
                      };
                      const normCls = (s) => (s || '').trim().replace(/\\s+/g,' ');
                      const wantClass = normCls(exactCls || '');
                      const isVisibleEnough = (el) => {
                        try {
                          const st = getComputedStyle(el);
                          if (st.display === 'none' || st.visibility === 'hidden') return false;
                          const op = parseFloat(st.opacity || '1');
                          if (op < 0.06) return false;
                          const r = el.getBoundingClientRect();
                          if (!r || r.width < 18 || r.height < 10) return false;
                          return true;
                        } catch (e) { return false; }
                      };
                      let c = 0;
                      const msg = root.querySelector('div[data-ad-preview="message"]');
                      const scope = msg || root;
                      const all = Array.from(scope.querySelectorAll('div[role="button"],span[role="button"],button,a[role="button"]')).slice(0, 1200);
                      for (const el of all) {
                        try {
                          if (!el || !el.isConnected) continue;
                          const t = normalize(el.innerText || el.textContent || '');
                          if (!isSeeMoreText(t)) continue;
                          if (!isVisibleEnough(el)) continue;
                          if (wantClass) {
                            const cls = normCls(el.getAttribute('class'));
                            if (cls !== wantClass) continue;
                          }
                          c += 1;
                        } catch (e) {}
                      }
                      return c;
                    }""",
                    SEE_MORE_BUTTON_CLASS,
                )
            )
        except Exception:
            return 0

    # Click multiple rounds because FB may reveal nested "see more" after the first click.
    for _ in range(8):
        n = _click_all_see_more_in_post()
        if n <= 0:
            # No clicks this round: consider done.
            break
        _wait_post_layout_settled(page, post, timeout_ms=2500)

    # STRICT: if still present, do not allow capture without expanding.
    left = _count_see_more_left()
    if left > 0:
        raise ElementTimeout(f"See-more still present in post after expansion attempts (left={left}).")


def _wait_post_layout_settled(page: Page, post, timeout_ms: int = 4000) -> None:
    """
    Wait until the post element's height becomes stable for a short window.
    This improves screenshots for long posts after expanding.
    """
    try:
        page.wait_for_function(
            """(el) => {
              if (!el) return false;
              const h1 = el.getBoundingClientRect().height;
              return new Promise(resolve => {
                requestAnimationFrame(() => {
                  const h2 = el.getBoundingClientRect().height;
                  resolve(Math.abs(h2 - h1) < 2);
                });
              });
            }""",
            post,
            timeout=timeout_ms,
        )
    except Exception:
        return


def _post_signature(post) -> str:
    """
    Build a more stable fingerprint to prevent duplicates/skips.
    Prefer permalink-like href inside the post; fallback to a hash of normalized text.
    """
    # 1) Try stable permalink/story ids if present
    try:
        links = post.locator("a[href]")
        for i in range(min(25, links.count())):
            href = links.nth(i).get_attribute("href", timeout=1200) or ""
            if not href:
                continue
            if not any(x in href for x in ["story_fbid", "/posts/", "/permalink/", "permalink.php"]):
                continue

            # Keep unique identifiers from query/path; do NOT drop query blindly.
            try:
                u = urlparse(href)
                qs = parse_qs(u.query)
                story_fbid = (qs.get("story_fbid") or [""])[0]
                pid = (qs.get("id") or [""])[0]
                fte = (qs.get("fbid") or [""])[0]
                if story_fbid:
                    return f"story_fbid:{story_fbid}|id:{pid}"
                if "permalink.php" in u.path and fte:
                    return f"permalink.php:{fte}|id:{pid}"
                # /posts/<id> or /permalink/<id>
                m = re.search(r"/posts/(\\d+)", u.path) or re.search(r"/permalink/(\\d+)", u.path)
                if m:
                    return f"path_id:{m.group(1)}"
            except Exception:
                continue
    except Exception:
        pass

    # 2) Try stable-ish data attributes used by FB
    try:
        # data-ft often contains top_level_post_id for feed stories (stringified JSON-ish)
        data_ft = post.get_attribute("data-ft", timeout=1200) or ""
        if data_ft:
            m = re.search(r'"top_level_post_id"\s*:\s*"(\d+)"', data_ft) or re.search(
                r"'top_level_post_id'\s*:\s*'(\d+)'", data_ft
            )
            if m:
                return f"top_level_post_id:{m.group(1)}"

        for attr in ["data-ft", "data-pagelet", "data-testid", "id"]:
            v = post.get_attribute(attr, timeout=1200) or ""
            if v and len(v) > 6:
                h = hashlib.sha1(v.encode("utf-8", errors="ignore")).hexdigest()[:10]
                return f"attr:{attr}|h:{h}"
    except Exception:
        pass

    # 3) Fallback: aria-posinset + hashed text (least stable)
    try:
        pos = post.get_attribute("aria-posinset", timeout=1200) or ""
    except Exception:
        pos = ""
    try:
        txt = post.inner_text(timeout=1200)
        txt = re.sub(r"\s+", " ", txt).strip()
    except Exception:
        txt = ""
    h = hashlib.sha1(txt[:800].encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"pos:{pos}|h:{h}"


def _apply_screenshot_overrides(page: Page) -> None:
    """
    Hide known floating/interactive overlays that can get captured inside post screenshots,
    e.g. reaction toolbars rendered as UL with specific classes.
    """
    css = """
    /* Reaction/toolbar overlay reported by user */
    ul.xuk3077.x78zum5.x1iyjqo2.xl56j7k.xe11lzi.x1vy8oqc.x88anuq { display: none !important; }

    /* Hide common sticky headers that can cover element screenshots */
    [role="banner"], div[role="banner"] { display:none !important; }
    header { display:none !important; }

    /* Hide lightweight hover UI that can overlap */
    [data-testid="hovercard"] { display:none !important; }

    /* Hide loading spinners/skeletons that sometimes overlay posts */
    [role="progressbar"] { display:none !important; }
    [aria-busy="true"] { cursor: default !important; }
    """
    try:
        page.add_style_tag(content=css)
    except Exception:
        pass
    # Avoid overly-broad CSS that can blank the page. Use JS to hide only truly "scrim-like" fixed overlays.
    try:
        scrim_white_op = float(_env_float("FB_SCRIM_WHITE_OP", 0.35))
        scrim_black_op = float(_env_float("FB_SCRIM_BLACK_OP", 0.12))
        page.evaluate(
            """([whiteOp, blackOp]) => {
              try {
                const vw = Math.max(document.documentElement.clientWidth, window.innerWidth || 0);
                const vh = Math.max(document.documentElement.clientHeight, window.innerHeight || 0);
                const nodes = Array.from(document.querySelectorAll('div,section,aside')).slice(0, 4000);
                for (const el of nodes) {
                  try {
                    const st = getComputedStyle(el);
                    if (st.position !== 'fixed') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < vw * 0.85 || r.height < vh * 0.85) continue;
                    const z = parseInt(st.zIndex || '0', 10) || 0;
                    if (z < 50) continue;
                    const bg = (st.backgroundColor || '').toLowerCase();
                    const op = parseFloat(st.opacity || '1');
                    const isScrim =
                      (bg.includes('255, 255, 255') && op >= whiteOp) ||
                      (bg.includes('0, 0, 0') && op >= blackOp);
                    if (isScrim) el.style.setProperty('display', 'none', 'important');
                  } catch (e) {}
                }
              } catch (e) {}
            }"""
            ,
            [scrim_white_op, scrim_black_op],
        )
    except Exception:
        pass


def _focus_and_dismiss_overlays(page: Page) -> None:
    """
    Best-effort dismiss transient overlays without stealing window focus.
    """
    try:
        page.keyboard.press("Escape", timeout=800)
    except Exception:
        pass


def _dismiss_white_scrim(page: Page) -> None:
    """
    Facebook sometimes shows a white/grey scrim overlay after scrolling.
    Best-effort: press Esc + hide obvious full-screen fixed overlays.
    """
    try:
        page.keyboard.press("Escape", timeout=800)
    except Exception:
        pass
    try:
        page.evaluate(
            """() => {
              // Ensure scrolling isn't locked by an overlay/modal state.
              try { document.documentElement.style.overflow = 'auto'; } catch (e) {}
              try { document.body && (document.body.style.overflow = 'auto'); } catch (e) {}
              const vw = Math.max(document.documentElement.clientWidth, window.innerWidth || 0);
              const vh = Math.max(document.documentElement.clientHeight, window.innerHeight || 0);
              const nodes = Array.from(document.querySelectorAll('div,section,aside'));
              for (const el of nodes) {
                const st = window.getComputedStyle(el);
                if (st.position !== 'fixed') continue;
                const r = el.getBoundingClientRect();
                if (r.width < vw * 0.85 || r.height < vh * 0.85) continue;
                const bg = (st.backgroundColor || '').toLowerCase();
                const op = parseFloat(st.opacity || '1');
                const looksWhite =
                  bg.includes('255, 255, 255') || bg.includes('248, 248, 248') || bg.includes('250, 250, 250');
                const looksDark = bg.includes('0, 0, 0') || bg.includes('16, 16, 16') || bg.includes('32, 32, 32');
                if ((looksWhite && op >= 0.6) || (looksDark && op >= 0.2)) {
                  el.style.setProperty('display', 'none', 'important');
                }
              }
              // Also remove any global pointer-event blockers.
              try { document.documentElement.style.pointerEvents = 'auto'; } catch (e) {}
              try { document.body && (document.body.style.pointerEvents = 'auto'); } catch (e) {}
            }"""
        )
    except Exception:
        pass


def _unblock_overlays(page: Page) -> None:
    """
    Stronger anti-overlay + scroll unlocker.
    Facebook sometimes shows a white scrim or modal state that stops scrolling.
    """
    # Esc is the safest way to close FB dialogs (reactions list, media lightbox, etc).
    # Press twice because FB sometimes swallows the first keypress during re-render.
    for _ in range(2):
        try:
            page.keyboard.press("Escape", timeout=800)
        except Exception:
            pass
        try:
            page.wait_for_timeout(80)
        except Exception:
            time.sleep(0.08)
    _dismiss_white_scrim(page)
    try:
        # IMPORTANT: do NOT click by coordinates here.
        # On FB search feed, "random" clicks can hit the reactions bar ("lượt thả cảm xúc")
        # and open overlays/dialogs which then lead to white screens / stuck scrolling.
        # Instead, restore focus safely via JS (no click).
        page.evaluate(
            """() => {
              try {
                const main = document.querySelector('div[role="main"]') || document.body;
                if (!main) return;
                // Make focusable and focus without scrolling.
                try { main.setAttribute('tabindex', '-1'); } catch (e) {}
                try { main.focus({ preventScroll: true }); } catch (e) { try { main.focus(); } catch (e2) {} }
                // Also focus the chosen primary scroller if any (wheel handlers can depend on it).
                const p = main.querySelector('[data-fbshot-primaryscroll="1"]');
                if (p) {
                  try { p.setAttribute('tabindex', '-1'); } catch (e) {}
                  try { p.focus({ preventScroll: true }); } catch (e) { try { p.focus(); } catch (e2) {} }
                }
              } catch (e) {}
            }"""
        )
    except Exception:
        pass
    try:
        page.evaluate(
            """() => {
              try { document.documentElement.style.overflow = 'auto'; } catch (e) {}
              try { document.body && (document.body.style.overflow = 'auto'); } catch (e) {}
              try { document.documentElement.style.pointerEvents = 'auto'; } catch (e) {}
              try { document.body && (document.body.style.pointerEvents = 'auto'); } catch (e) {}
              // If a reactions dialog is open, hide it (it's not needed for capture and blocks scrolling).
              try {
                const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).slice(0, 20);
                for (const d of dialogs) {
                  try {
                    const t = ((d.innerText || '') + ' ' + (d.getAttribute('aria-label') || '')).toLowerCase();
                    if (t.includes('cảm xúc') || t.includes('reaction') || t.includes('reactions')) {
                      d.style.setProperty('display', 'none', 'important');
                      d.style.setProperty('pointer-events', 'none', 'important');
                    }
                  } catch (e) {}
                }
              } catch (e) {}
              // Hide large fixed overlays with very high z-index (common for scrims).
              const vw = Math.max(document.documentElement.clientWidth, window.innerWidth || 0);
              const vh = Math.max(document.documentElement.clientHeight, window.innerHeight || 0);
              const nodes = Array.from(document.querySelectorAll('div,section,aside')).slice(0, 5000);
              for (const el of nodes) {
                try {
                  const st = getComputedStyle(el);
                  if (st.position !== 'fixed') continue;
                  const z = parseInt(st.zIndex || '0', 10) || 0;
                  const r = el.getBoundingClientRect();
                  if (r.width < vw * 0.85 || r.height < vh * 0.85) continue;
                  const bg = (st.backgroundColor || '').toLowerCase();
                  const op = parseFloat(st.opacity || '1');
                  const isScrim = (bg.includes('255, 255, 255') && op >= 0.4) || (bg.includes('0, 0, 0') && op >= 0.12);
                  if (isScrim && z >= 100) {
                    el.style.setProperty('display', 'none', 'important');
                  }
                } catch (e) {}
              }

              // If the page looks "all white", try to dismiss by clicking the top-most full-screen fixed element.
              try {
                const candidates = Array.from(document.querySelectorAll('div,section,aside')).filter(el => {
                  try {
                    const st = getComputedStyle(el);
                    if (st.position !== 'fixed') return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < vw * 0.85 || r.height < vh * 0.85) return false;
                    const z = parseInt(st.zIndex || '0', 10) || 0;
                    if (z < 50) return false;
                    // Must be able to intercept clicks (scrim that requires clicking to dismiss)
                    if ((st.pointerEvents || '').toLowerCase() === 'none') return false;
                    return true;
                  } catch (e) { return false; }
                }).slice(0, 20);
                // Try hiding the topmost first
                candidates.sort((a,b) => {
                  const za = parseInt(getComputedStyle(a).zIndex || '0', 10) || 0;
                  const zb = parseInt(getComputedStyle(b).zIndex || '0', 10) || 0;
                  return zb - za;
                });
                if (candidates.length) {
                  // Click center of overlay first (often required to dismiss)
                  try {
                    const el = candidates[0];
                    const r = el.getBoundingClientRect();
                    const x = Math.floor(r.left + r.width * 0.5);
                    const y = Math.floor(r.top + r.height * 0.35);
                    el.dispatchEvent(new MouseEvent('mousedown', { bubbles:true, cancelable:true, clientX:x, clientY:y }));
                    el.dispatchEvent(new MouseEvent('mouseup', { bubbles:true, cancelable:true, clientX:x, clientY:y }));
                    el.dispatchEvent(new MouseEvent('click', { bubbles:true, cancelable:true, clientX:x, clientY:y }));
                  } catch (e) {}
                  candidates[0].style.setProperty('display', 'none', 'important');
                }
              } catch (e) {}
            }"""
        )
    except Exception:
        pass


def _wait_post_media_ready(page: Page, post, timeout_ms: int = 8000) -> bool:
    """
    Wait a bit for images inside the post to finish decoding so we don't capture
    blurred placeholders/skeletons.
    """
    try:
        page.wait_for_function(
            """(el) => {
              if (!el) return true;
              const imgs = Array.from(el.querySelectorAll('img'));
              if (imgs.length === 0) return true;
              // Consider ready when most images are complete and have dimensions.
              let ok = 0;
              for (const im of imgs) {
                if (im.complete && im.naturalWidth > 40 && im.naturalHeight > 40) ok++;
              }
              return ok >= Math.max(1, Math.floor(imgs.length * 0.7));
            }""",
            post,
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def _wait_post_not_loading(page: Page, post, timeout_ms: int = 6000) -> bool:
    """
    Avoid capturing skeleton/loading UI inside a post.
    Wait until the post has no progressbar and is not aria-busy.
    """
    try:
        page.wait_for_function(
            """(el) => {
              if (!el) return true;
              // FB sometimes keeps aria-busy=true even when content is visible; don't hard-fail solely on that.
              const busy = el.getAttribute('aria-busy') === 'true';
              if (el.querySelector('[role="progressbar"]')) return false;
              const t = (el.innerText || '').toLowerCase();
              if (t.includes('đang tải') || t.includes('loading')) return false;
              // If we already have meaningful content, consider it "not loading" even if aria-busy is true.
              const hasText = (el.innerText || '').replace(/\\s+/g,' ').trim().length >= 20;
              const hasPermalink = !!el.querySelector('a[href*="story_fbid"],a[href*="/posts/"],a[href*="permalink"],a[href*="permalink.php"]');
              const hasImg = !!el.querySelector('img');
              if (hasText || hasPermalink || hasImg) return true;
              return !busy;
            }""",
            post,
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def _looks_like_skeleton(post) -> bool:
    """
    Fast skeleton detection to avoid capturing placeholder cards.
    """
    try:
        return bool(
            post.evaluate(
                """(el) => {
                  if (!el) return true;
                  if (el.getAttribute('aria-busy') === 'true') return true;
                  if (el.querySelector('[role="progressbar"]')) return true;
                  // Common skeleton patterns: lots of grey blocks and no real text
                  const t = (el.innerText || '').trim();
                  const tl = t.toLowerCase();
                  if (tl.includes('đang tải') || tl.includes('loading')) return true;
                  // If it already looks like a real post (permalink anchors), do NOT treat as skeleton.
                  if (el.querySelector('a[href*="story_fbid"],a[href*="/posts/"],a[href*="permalink"],a[href*="permalink.php"]')) return false;
                  // Heuristic: skeleton "card" often has very few characters but many divs.
                  const divs = el.querySelectorAll('div').length;
                  if (t.length < 8 && divs > 30) return true;
                  return false;
                }""",
                timeout=1200,
            )
        )
    except Exception:
        return False


def _looks_like_real_post(post) -> bool:
    """
    Distinguish real post cards from placeholder/skeleton containers.
    We only "must capture every post" for real posts, not skeleton placeholders.
    """
    try:
        return bool(
            post.evaluate(
                """(el) => {
                  if (!el) return false;
                  // Must have some meaningful text
                  const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                  if (t.length < 25) return false;
                  // Prefer: has permalink-ish anchors (search result posts often include these)
                  const a = el.querySelector('a[href*="story_fbid"],a[href*="/posts/"],a[href*="permalink"],a[href*="permalink.php"]');
                  if (a) return true;
                  // Fallback: any anchor with href and enough text content
                  const anyA = el.querySelector('a[href]');
                  return !!anyA;
                }"""
            )
        )
    except Exception:
        return False


def _is_in_viewport(post) -> bool:
    """
    Capture only when element is inside viewport to avoid scroll-down-then-scroll-up behavior.
    Uses DOM rect via evaluate (safer than Playwright bounding_box()).
    """
    try:
        return bool(
            post.evaluate(
                """(el) => {
                  if (!el) return false;
                  const r = el.getBoundingClientRect();
                  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
                  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
                  if (vh <= 0 || vw <= 0) return false;
                  const visibleH = Math.min(r.bottom, vh) - Math.max(r.top, 0);
                  const visibleW = Math.min(r.right, vw) - Math.max(r.left, 0);
                  // Be permissive: FB layouts can be tall; requiring 60% can skip everything.
                  const okH = visibleH >= Math.min(r.height * 0.3, vh * 0.35);
                  const okW = visibleW >= Math.min(r.width * 0.6, vw * 0.6);
                  return okH && okW;
                }""",
                timeout=1200,
            )
        )
    except Exception:
        return False


def _debug_post_selectors(page: Page, log: Callable[[str, str], None]) -> None:
    """
    When we can't find any post elements, log some cheap DOM counters so we can
    adapt selectors to FB layout changes (esp. /search/posts).
    """
    try:
        info = page.evaluate(
            """() => {
              const main = document.querySelector('div[role="main"]');
              const q = (sel) => (main ? main.querySelectorAll(sel).length : 0);
              return {
                hasMain: !!main,
                roleArticle: q('[role="article"]'),
                divRoleArticle: q('div[role="article"]'),
                articleTag: q('article'),
                ariaPosinset: q('[aria-posinset]'),
                feedUnit: q('div[data-pagelet*="FeedUnit_"]'),
                anyPagelet: q('div[data-pagelet]'),
              };
            }"""
        )
        log("capture", f"Selector debug: {info}")
    except Exception as e:
        try:
            log("capture", f"Selector debug failed: {e}")
        except Exception:
            pass


def _wait_posts_present(page: Page, log: Callable[[str, str], None], timeout_s: float = 25.0) -> bool:
    """
    Viewport mode: wait until real posts appear to avoid capturing blank/skeleton pages.
    """
    deadline = time.time() + max(3.0, timeout_s)
    last_log = 0.0
    last_debug = 0.0
    while time.time() < deadline:
        try:
            ok = bool(
                page.evaluate(
                    """() => {
                      const main = document.querySelector('[role="main"]') || document.querySelector('div[role="feed"]') || document.body;
                      if (!main) return false;
                      // If FB has rendered article-like units, we consider "posts present".
                      const nodes = Array.from(
                        main.querySelectorAll('[role="article"],div[role="article"],article,div[data-pagelet*="FeedUnit_"],div[data-pagelet],div[data-ad-preview]')
                      );
                      // Fast path: aria-posinset is a strong signal results are present.
                      const pos = main.querySelectorAll('[aria-posinset]').length;
                      if (pos > 0) return true;
                      if (nodes.length <= 0) return false;

                      // Avoid pure skeleton screens: if main has only progressbars / busy states, keep waiting.
                      const hasProgress = !!main.querySelector('[role="progressbar"]');
                      const busyMain = main.getAttribute('aria-busy') === 'true';
                      const txt = (main.innerText || '').replace(/\\s+/g,' ').trim();

                      // Many real posts are image-heavy with little text; accept if there is media or a permalink-ish link.
                      let realLike = 0;
                      for (const el of nodes.slice(0, 12)) {
                        if (!el) continue;
                        if (el.getAttribute && el.getAttribute('aria-busy') === 'true') continue;
                        if (el.querySelector && el.querySelector('[role="progressbar"]')) continue;
                        const hasPermalink = !!(el.querySelector && el.querySelector('a[href*="story_fbid"],a[href*="/posts/"],a[href*="permalink"],a[href*="permalink.php"]'));
                        const hasMedia = !!(el.querySelector && el.querySelector('img,video'));
                        const et = (el.innerText || '').replace(/\\s+/g,' ').trim();
                        if (hasPermalink || hasMedia || et.length >= 12) realLike++;
                      }

                      // If we found at least one real-looking unit, good.
                      if (realLike >= 1) return true;

                      // Fallback: if there is some non-trivial text and not obviously loading, accept.
                      if (!hasProgress && !busyMain && txt.length >= 15) return true;
                      // Last fallback: media-heavy layouts sometimes have little text; accept if main has any media.
                      if (!hasProgress && !busyMain && main.querySelector('img,video')) return true;
                      return false;
                    }""",
                )
            )
        except Exception:
            ok = False

        if ok:
            return True

        if time.time() - last_log >= 3.5:
            last_log = time.time()
            try:
                log("capture", "Waiting for posts to appear (avoid blank capture)…")
            except Exception:
                pass

        # Extra debug when user reports posts are visible but detector says none.
        if time.time() - last_debug >= 7.0:
            last_debug = time.time()
            try:
                info = page.evaluate(
                    """() => {
                      const main = document.querySelector('div[role="main"]');
                      const q = (sel) => (main ? main.querySelectorAll(sel).length : 0);
                      const url = (location && location.href) ? String(location.href) : '';
                      const title = (document.title || '');
                      const bodyHead = (document.body && (document.body.innerText||'')) ? (document.body.innerText||'').slice(0, 220) : '';
                      const hasLoginInputs = !!document.querySelector('input[name="email"],input#email,input[name="pass"],input#pass,input[type="password"]');
                      const cookieTxt = bodyHead.toLowerCase();
                      const consentLike = cookieTxt.includes('cookie') || cookieTxt.includes('chấp nhận') || cookieTxt.includes('đồng ý');
                      const first = (sel) => {
                        if (!main) return null;
                        const el = main.querySelector(sel);
                        if (!el) return null;
                        const t = (el.innerText || '').replace(/\\s+/g,' ').trim();
                        return {
                          tag: (el.tagName||'').toLowerCase(),
                          role: (el.getAttribute('role')||''),
                          textLen: t.length,
                          hasImg: !!el.querySelector('img'),
                          hasVideo: !!el.querySelector('video'),
                          hasLink: !!el.querySelector('a[href]'),
                          hasPermalink: !!el.querySelector('a[href*=\"story_fbid\"],a[href*=\"/posts/\"],a[href*=\"permalink\"],a[href*=\"permalink.php\"]')
                        };
                      };
                      return {
                        hasMain: !!main,
                        url,
                        title,
                        hasLoginInputs,
                        consentLike,
                        bodyHead,
                        roleArticle: q('[role=\"article\"]'),
                        divRoleArticle: q('div[role=\"article\"]'),
                        articleTag: q('article'),
                        feedUnit: q('div[data-pagelet*=\"FeedUnit_\"]'),
                        anyPagelet: q('div[data-pagelet]'),
                        adPreview: q('div[data-ad-preview]'),
                        imgs: q('img'),
                        videos: q('video'),
                        progress: q('[role=\"progressbar\"]'),
                        busy: main ? (main.getAttribute('aria-busy')||'') : '',
                        firstArticle: first('[role=\"article\"],div[role=\"article\"],article,div[data-pagelet]')
                      };
                    }""",
                )
                log("capture", f"Post-detect debug: {info}")
            except Exception as e:
                try:
                    log("capture", f"Post-detect debug failed: {e}")
                except Exception:
                    pass

        try:
            page.wait_for_timeout(350)
        except Exception:
            time.sleep(0.35)
    return False


def _expand_see_more_in_viewport(page: Page, log: Callable[[str, str], None]) -> None:
    """
    Viewport mode: best-effort click visible "Xem thêm" / "See more" buttons
    so the screenshot includes full content.
    """
    try:
        # Prefer the exact "Xem thêm" element class provided by user (most stable for this account/layout).
        page.evaluate(
            """(cls) => { window.__fbshotSeeMoreExactClass = cls; }""",
            "x1i10hfl xjbqb8w x1ejq31n x18oe1m7 x1sy0etr xstzfhl x972fbf x10w94by x1qhh985 x14e42zd x9f619 x1ypdohk xt0psk2 x3ct3a4 xdj266r x14z9mp xat24cr x1lziwak xexx8yu xyri2b x18d9i69 x1c1uobl x16tdsg8 x1hl2dhg xggy1nq x1a2a7pz xkrqix3 x1sur9pj xzsf02u x1s688f",
            timeout=1200,
        )
    except Exception:
        pass
    try:
        # Click-by-JS is much more reliable on FB than locator.click()
        # (avoids overlays, focus stealing, and nested scroll quirks).
        def _count_visible() -> int:
            try:
                return int(
                    page.evaluate(
                        """() => {
                          const vh = window.innerHeight || document.documentElement.clientHeight || 0;
                          const vw = window.innerWidth || document.documentElement.clientWidth || 0;
                          if (vh <= 0 || vw <= 0) return 0;
                          const main = document.querySelector('div[role="main"]') || document.body;
                          if (!main) return 0;
                          const isInViewport = (el) => {
                            const r = el.getBoundingClientRect();
                            return r.bottom > 40 && r.top < vh - 40 && r.right > 30 && r.left < vw - 30;
                          };
                          const nodes = main.querySelectorAll('button,[role="button"],span,div');
                          let c = 0;
                          for (const el of nodes) {
                            if (!el) continue;
                            if (!isInViewport(el)) continue;
                            const t = (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim().toLowerCase();
                            if (!t) continue;
                            if (t === 'xem thêm' || t === 'see more' || t.startsWith('xem thêm') || t.startsWith('see more')) c++;
                          }
                          return c;
                        }""",
                        timeout=2000,
                    )
                )
            except Exception:
                return 0

        total_clicked = 0
        for _round in range(16):
            _dismiss_white_scrim(page)
            try:
                clicked = int(
                    page.evaluate(
                        """() => {
                          const vh = window.innerHeight || document.documentElement.clientHeight || 0;
                          const vw = window.innerWidth || document.documentElement.clientWidth || 0;
                          if (vh <= 0 || vw <= 0) return 0;

                          const isInViewport = (el) => {
                            const r = el.getBoundingClientRect();
                            return r.bottom > 40 && r.top < vh - 40 && r.right > 30 && r.left < vw - 30;
                          };

                          const findClickable = (el) => {
                            if (!el) return null;
                            const directRole = (el.getAttribute && (el.getAttribute('role') || '').toLowerCase()) || '';
                            const directTag = (el.tagName || '').toLowerCase();
                            if (directTag === 'button' || directRole === 'button') return el;
                            const p = el.closest && el.closest('button,[role="button"]');
                            return p || el;
                          };

                          const looksClickable = (el) => {
                            const role = (el.getAttribute('role') || '').toLowerCase();
                            if (role === 'button') return true;
                            const tag = (el.tagName || '').toLowerCase();
                            if (tag === 'button') return true;
                            // FB often uses div/span with click handlers
                            return typeof el.onclick === 'function';
                          };

                          const candidates = [];
                          const main = document.querySelector('div[role="main"]') || document.body;
                          if (!main) return 0;
                          const exactCls = (window.__fbshotSeeMoreExactClass || '').trim();
                          const byExactClass = exactCls
                            ? Array.from(main.querySelectorAll('div')).filter(el => {
                                try {
                                  const cls = (el.getAttribute('class') || '').trim().replace(/\\s+/g,' ');
                                  return cls === exactCls;
                                } catch (e) { return false; }
                              })
                            : [];
                          const nodes = byExactClass.length ? byExactClass : Array.from(main.querySelectorAll('div[role="button"],button,span,div'));
                          for (const el of nodes) {
                            if (!el) continue;
                            if (!isInViewport(el)) continue;
                            const t = (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim().toLowerCase();
                            if (!t) continue;
                            // Accept variants like "Xem thêm…" / "See more…" / "Xem thêm nữa"
                            if (!(t === 'xem thêm' || t === 'see more' || t.startsWith('xem thêm') || t.startsWith('see more'))) continue;
                            const target = findClickable(el);
                            if (!target) continue;
                            if (!looksClickable(target)) continue;
                            candidates.push(target);
                          }

                          // Click up to a few per round to avoid runaway loops.
                          let clicked = 0;
                          for (const el of candidates.slice(0, 4)) {
                            try {
                              el.click();
                              clicked++;
                            } catch (e) {}
                          }
                          return clicked;
                        }""",
                        timeout=2500,
                    )
                )
            except Exception:
                clicked = 0

            if clicked <= 0:
                break

            total_clicked += clicked
            try:
                log("capture", f'Expanded "Xem thêm": +{clicked} (total {total_clicked})')
            except Exception:
                pass

            # Give FB time to reflow after expansion.
            try:
                page.wait_for_timeout(350)
            except Exception:
                time.sleep(0.35)

        # Hard guarantee: if still visible, keep trying a bit more before screenshot.
        # (Capture loop will call this right before screenshot.)
        if _count_visible() > 0:
            try:
                log("capture", 'See-more still visible after expansion attempts.')
            except Exception:
                pass
    except Exception as e:
        try:
            log("capture", f'See-more expand failed (ignored): {e}')
        except Exception:
            pass


def capture_posts(
    page: Page,
    params: RunParams,
    posts_root: Path,
    progress: Callable[[int, int], None],
    log: Callable[[str, str], None],
    should_cancel: Callable[[], bool],
    expected_search_url: str | None = None,
) -> int:
    day = local_date_yyyy_mm_dd()
    kw_dir = sanitize_keyword_for_path(params.keyword)
    base_dir = posts_root / day / kw_dir
    base_dir.mkdir(parents=True, exist_ok=True)

    # Each run gets its own folder: "Lan N - HHMM"
    # This keeps numbering independent per keyword run and avoids mixing screenshots across sessions.
    def _pick_run_dir(parent: Path) -> Path:
        run_no = 1
        try:
            for p in parent.iterdir():
                if not p.is_dir():
                    continue
                m = re.match(r"^Lan\s+(\d+)\s+-\s+(\d{4})$", p.name.strip(), flags=re.IGNORECASE)
                if m:
                    run_no = max(run_no, int(m.group(1)) + 1)
        except Exception:
            run_no = 1
        hhmm = time.strftime("%H%M")
        return parent / f"Lan {run_no} - {hhmm}"

    out_dir = _pick_run_dir(base_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    next_index = 1

    saved = 0
    unlimited = int(params.max_posts) <= 0
    if unlimited:
        log("capture", "Start capturing all posts until end of results.")
        progress(0, -1)
    else:
        log("capture", f"Start capturing up to {params.max_posts} posts.")
        progress(0, params.max_posts)

    try:
        log("capture", f"Output folder: {out_dir.name}")
    except Exception:
        pass

    _apply_screenshot_overrides(page)
    _focus_and_dismiss_overlays(page)
    _dismiss_white_scrim(page)
    # If we got redirected away (login/checkpoint/consent) between search->capture, self-heal here.
    # This is critical because `_wait_posts_present` relies on `div[role="main"]` which doesn't exist
    # on some login/consent/error pages.
    try:
        cur = (page.url or "").lower()
    except Exception:
        cur = ""
    try:
        has_login = page.locator('input[name="email"], input#email, input[name="pass"], input#pass, input[type="password"]').count() > 0
    except Exception:
        has_login = False
    if (("login" in cur) or has_login) and not _is_logged_in(page):
        try:
            log("capture", "Detected login page during capture. Re-login and return to search URL…", "WARN")
        except Exception:
            pass
        try:
            page = ensure_login(page, params, log)
        except Exception:
            # Let normal error handling retry the job.
            raise
    if expected_search_url:
        try:
            page = _ensure_open_page(page)
            page.goto(expected_search_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            pass


    # Expose the ONLY allowed click target class to the page (strict allowlist).
    # We will use this in a JS-level click guard to prevent any accidental clicks
    # on "Tham gia nhóm" / reactions / interaction summaries.
    # Note: allowlist is based on role+text ("xem thêm") now; no need to pass class to the page.

    # Hard safety: prevent accidental navigation to photo/lightbox pages while capturing.
    # Even with DOM-based actions, FB sometimes triggers document navigations (e.g. mis-click on an image/link).
    # We block document requests to /photo/ (and close variants) so capture never "loses" the search feed.
    _nav_guard_installed = {"ok": False}
    _nav_guard_patterns: list[str] = []
    _nav_guard_handlers: list[object] = []

    def _install_photo_nav_guard() -> None:
        try:
            # Also guard in-page clicks to photo anchors (capture phase).
            try:
                page.evaluate(
                    """() => {
                      if (window.__fbshotPhotoClickGuard) return;
                      window.__fbshotPhotoClickGuard = true;
                      // Hard disable pointer events on reaction/reactions entrypoints.
                      // This is the most reliable way to prevent opening the "lượt thả cảm xúc" dialog.
                      if (!window.__fbshotNoReactionsCss) {
                        window.__fbshotNoReactionsCss = true;
                        const st = document.createElement('style');
                        st.textContent = `
                          a[href*="ufi/reaction"], a[href*="reaction"], a[href*="reactions"] { pointer-events: none !important; }
                          [aria-label*="Cảm xúc"], [aria-label*="cảm xúc"], [aria-label*="Reactions"], [aria-label*="reactions"] { pointer-events: none !important; }
                        `;
                        (document.head || document.documentElement).appendChild(st);
                      }

                      // Click shield: block ANY clicks that can open overlays or navigate away during capture.
                      // This includes the reactions bar ("lượt thả cảm xúc") which opens a dialog and can
                      // lead to scrims/white screens and broken scrolling.
                      if (!window.__fbshotClickShield) {
                        window.__fbshotClickShield = true;
                        const shouldBlock = (t) => {
                          try {
                            if (!t) return false;
                            const el = t.closest ? t.closest('a,div[role="button"],span[role="button"],button') : null;
                            if (!el) return false;
                            const href = ((el.getAttribute && el.getAttribute('href')) || '').toLowerCase();
                            const aria = ((el.getAttribute && el.getAttribute('aria-label')) || '').toLowerCase();
                            const txt = ((el.innerText || '') + ' ' + (el.textContent || '')).toLowerCase();
                            return (
                              href.includes('ufi/reaction') ||
                              href.includes('reaction') ||
                              aria.includes('reaction') ||
                              aria.includes('reactions') ||
                              aria.includes('cảm xúc') ||
                              aria.includes('thả cảm xúc') ||
                              aria.includes('tham gia nhóm') ||
                              aria.includes('join group') ||
                              txt.includes('tham gia nhóm') ||
                              txt.includes('join group') ||
                              txt.includes('lượt tương tác') ||
                              txt.includes('interactions') ||
                              txt.includes('cảm xúc') ||
                              txt.includes('reactions')
                            );
                          } catch (e) { return false; }
                        };
                        const stop = (e) => { try { e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation && e.stopImmediatePropagation(); } catch (err) {} };
                        // Block early pointer events too (FB can open reactions on press, not only click).
                        document.addEventListener('pointerdown', (e) => { try { if (shouldBlock(e && e.target)) stop(e); } catch (err) {} }, true);
                        document.addEventListener('mousedown', (e) => { try { if (shouldBlock(e && e.target)) stop(e); } catch (err) {} }, true);
                        document.addEventListener('touchstart', (e) => { try { if (shouldBlock(e && e.target)) stop(e); } catch (err) {} }, true);
                        document.addEventListener('click', (e) => {
                          try {
                            if (shouldBlock(e && e.target)) stop(e);
                          } catch (err) {}
                        }, true);
                      }
                      document.addEventListener('click', (e) => {
                        try {
                          const t = e && e.target ? e.target : null;
                          if (!t) return;
                          const a = t.closest ? t.closest('a[href]') : null;
                          if (!a) return;
                          const href = (a.getAttribute('href') || '').toLowerCase();
                          // Block common navigations away from /search/ that break capture:
                          // - photo/lightbox (/photo, fbid=)
                          // - direct post links (/posts/, /permalink/, story_fbid, pfbid)
                          if (
                            href.includes('/photo') ||
                            href.includes('fbid=') ||
                            href.includes('/posts/') ||
                            href.includes('/permalink/') ||
                            href.includes('permalink.php') ||
                            href.includes('story_fbid=') ||
                            href.includes('/story.php') ||
                            href.includes('pfbid')
                          ) {
                            e.preventDefault();
                            e.stopPropagation();
                            e.stopImmediatePropagation && e.stopImmediatePropagation();
                          }
                        } catch (err) {}
                      }, true);
                    }"""
                )
            except Exception:
                pass

            def _handler(route, request):
                try:
                    if (request.resource_type or "").lower() != "document":
                        return route.continue_()
                    u = (request.url or "").lower()
                    # Hard-block document navigations that pull us out of /search/ during capture.
                    if (
                        "/photo" in u
                        or "facebook.com/photo" in u
                        or "/posts/" in u
                        or "/permalink/" in u
                        or "permalink.php" in u
                        or "story_fbid=" in u
                        or "/story.php" in u
                        or "pfbid" in u
                    ):
                        return route.abort()
                except Exception:
                    try:
                        return route.continue_()
                    except Exception:
                        return
                return route.continue_()

            patterns = [
                "**/photo/**",
                "**/photo/?**",
                "**/photo.php?**",
                "**/posts/**",
                "**/permalink/**",
                "**/permalink.php?**",
                "**/story.php?**",
            ]
            for pat in patterns:
                try:
                    page.route(pat, _handler)
                    _nav_guard_patterns.append(pat)
                    _nav_guard_handlers.append(_handler)
                except Exception:
                    continue
            _nav_guard_installed["ok"] = True
        except Exception:
            _nav_guard_installed["ok"] = False

    def _uninstall_photo_nav_guard() -> None:
        if not _nav_guard_installed.get("ok"):
            return
        for pat, h in zip(_nav_guard_patterns, _nav_guard_handlers):
            try:
                page.unroute(pat, h)
            except Exception:
                continue

    _install_photo_nav_guard()

    # IMPORTANT (per user requirement):
    # Use DOM-based capture by post order using `aria-posinset`.
    # Example: div[aria-posinset="98"] is the 98th post result.
    # We scroll to each posinset sequentially and take an element screenshot (not viewport hashing).
    def _wait_posts_present_or_fail() -> None:
        try:
            ok = _wait_posts_present(page, log, timeout_s=22.0)
        except Exception:
            ok = False
        if ok:
            return
        # light recovery: small scroll + extra wait (avoid reload loops)
        try:
            log("capture", "No posts detected yet. Trying light scroll + extra wait…", "WARN")
        except Exception:
            pass
        try:
            for _ in range(3):
                if should_cancel():
                    break
                try:
                    page.mouse.wheel(0, 1200)
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    time.sleep(0.25)
        except Exception:
            pass
        try:
            ok2 = _wait_posts_present(page, log, timeout_s=28.0)
        except Exception:
            ok2 = False
        if not ok2:
            raise ElementTimeout("No posts rendered on search results (after wait + extra wait).")

    _wait_posts_present_or_fail()

    # Scroll strategy (permanent fix):
    # Facebook often uses an inner scroll container. If we force window-only scroll, it will
    # eventually "stop" (window.scrollY won't change) while results still exist, causing reload loops.
    # We therefore enforce exactly ONE active scroller:
    # - Prefer the main inner scroller when it exists (and hide/disable all other scrollbars)
    # - Otherwise fall back to window/document scrolling.
    def _enforce_single_scroller() -> None:
        try:
            page.evaluate(
                """() => {
                  const main = document.querySelector('div[role="main"]') || document.body;
                  if (!main) return;
                  // Install CSS once to hide scrollbars on elements we mark.
                  if (!window.__fbshotNoDivScrollCss) {
                    window.__fbshotNoDivScrollCss = true;
                    const style = document.createElement('style');
                    style.textContent = `
                      [data-fbshot-noscroll="1"] { scrollbar-width: none !important; -ms-overflow-style: none !important; }
                      [data-fbshot-noscroll="1"]::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }
                      /* Hard visual guarantee: hide all inner scrollbars under main */
                      div[role="main"] *::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }
                      div[role="main"] * { scrollbar-width: none !important; -ms-overflow-style: none !important; }
                      /* Allow the chosen primary scroller to show a normal scrollbar */
                      [data-fbshot-primaryscroll="1"] { scrollbar-width: auto !important; -ms-overflow-style: auto !important; }
                      [data-fbshot-primaryscroll="1"]::-webkit-scrollbar { width: initial !important; height: initial !important; display: initial !important; }
                    `;
                    document.head && document.head.appendChild(style);
                  }
                  // Disable any scrollable container under main (and a few common wrappers),
                  // keeping ONLY ONE scroller active.
                  const nodes = [main, ...Array.from(main.querySelectorAll('div,section,main,aside,nav')).slice(0, 1200)];

                  // Find best scrollable inner container.
                  let primary = null;
                  let bestScore = -1;
                  for (const el of nodes) {
                    try {
                      if (!el || el === document.body || el === document.documentElement) continue;
                      const st = getComputedStyle(el);
                      const oy = st.overflowY || '';
                      const scrollable = (oy === 'auto' || oy === 'scroll');
                      if (!scrollable) continue;
                      const ch = el.clientHeight || 0;
                      const sh = el.scrollHeight || 0;
                      if (ch < 260) continue;
                      if (sh <= ch + 300) continue;
                      const score = (sh - ch) + ch;
                      if (score > bestScore) { bestScore = score; primary = el; }
                    } catch (e) {}
                  }

                  // Mark primary and disable all other inner scrollers.
                  for (const el of nodes) {
                    try {
                      if (!el || el === document.body || el === document.documentElement) continue;
                      const st = getComputedStyle(el);
                      const oy = st.overflowY || '';
                      const scrollable = (oy === 'auto' || oy === 'scroll');
                      if (!scrollable) continue;
                      const ch = el.clientHeight || 0;
                      const sh = el.scrollHeight || 0;
                      if (ch < 200) continue;
                      if (sh <= ch + 200) continue;
                      // Don't touch form controls.
                      const tag = (el.tagName || '').toLowerCase();
                      if (tag === 'textarea' || tag === 'select' || tag === 'input') continue;
                      if (primary && el === primary) {
                        el.setAttribute('data-fbshot-primaryscroll', '1');
                        el.style.setProperty('overflow-y', 'auto', 'important');
                        el.style.setProperty('overscroll-behavior', 'contain', 'important');
                      } else {
                        el.removeAttribute('data-fbshot-primaryscroll');
                        el.scrollTop = 0;
                        el.style.setProperty('overflow-y', 'hidden', 'important');
                        el.style.setProperty('overscroll-behavior', 'none', 'important');
                        el.setAttribute('data-fbshot-noscroll', '1');
                      }
                    } catch (e) {}
                  }

                  // If we found a primary inner scroller, lock window scrolling to prevent 2 scrollbars.
                  try {
                    if (primary) {
                      document.documentElement.style.overflowY = 'hidden';
                      document.body && (document.body.style.overflowY = 'hidden');
                    } else {
                      document.documentElement.style.overflowY = 'auto';
                      document.body && (document.body.style.overflowY = 'auto');
                    }
                  } catch (e) {}
                }"""
            )
        except Exception:
            pass

    def _scroll_diag() -> dict:
        try:
            return page.evaluate(
                """() => {
                  const se = document.scrollingElement || document.documentElement;
                  const y = window.scrollY || 0;
                  const docTop = se ? (se.scrollTop || 0) : 0;
                  const docH = se ? (se.scrollHeight || 0) : (document.body ? (document.body.scrollHeight||0) : 0);
                  const main = document.querySelector('div[role="main"]') || document.body;
                  const q = (sel) => (main ? main.querySelectorAll(sel).length : 0);
                  const primary = main ? main.querySelector('[data-fbshot-primaryscroll="1"]') : null;
                  const primaryTop = primary ? (primary.scrollTop||0) : 0;
                  const primaryCH = primary ? (primary.clientHeight||0) : 0;
                  const primarySH = primary ? (primary.scrollHeight||0) : 0;
                  // Count large scrollable inner containers (should be 0 except primary)
                  let inner = 0;
                  if (main) {
                    const nodes = Array.from(main.querySelectorAll('div,section,main')).slice(0, 700);
                    for (const el of nodes) {
                      try {
                        const st = getComputedStyle(el);
                        const oy = st.overflowY || '';
                        if (oy !== 'auto' && oy !== 'scroll') continue;
                        const ch = el.clientHeight || 0;
                        const sh = el.scrollHeight || 0;
                        if (ch < 200) continue;
                        if (sh <= ch + 200) continue;
                        inner++;
                      } catch (e) {}
                    }
                  }
                  const bodyOverflow = document.body ? (getComputedStyle(document.body).overflowY || '') : '';
                  const htmlOverflow = getComputedStyle(document.documentElement).overflowY || '';
                  return { y, docTop, docH, primaryTop, primaryCH, primarySH, innerScrollables: inner, htmlOverflow, bodyOverflow, articles: q('[role="article"],div[data-pagelet*="FeedUnit_"],article') };
                }"""
            ) or {}
        except Exception:
            return {}

    def _scroll_down_and_verify() -> bool:
        """
        Scroll the single active scroller (primary inner if present; else window).
        Verify progress and avoid false "stuck" reloads.
        """
        _enforce_single_scroller()
        _dismiss_white_scrim(page)
        d0 = _scroll_diag()
        before_win = float(d0.get("y") or 0.0)
        before_primary = float(d0.get("primaryTop") or 0.0)
        has_primary = float(d0.get("primarySH") or 0.0) > float(d0.get("primaryCH") or 0.0) + 50.0
        before_primary_sh = float(d0.get("primarySH") or 0.0)
        before_primary_ch = float(d0.get("primaryCH") or 0.0)

        # Multi-strategy scroll (human-like / touchpad-friendly):
        # 1) Direct scrollTop increment on primary scroller (most reliable)
        # 2) scrollIntoView(last item) to put feed in focus
        # 3) dispatch wheel events (touchpad-like)
        # 4) fallback keyboard PageDown
        try:
            scroll_factor = float(_env_float("FB_SCROLL_STEP_FACTOR", 0.85))
            scroll_factor = max(0.55, min(0.95, scroll_factor))
            wheel_steps = int(_env_float("FB_WHEEL_STEPS", 3.0))
            wheel_steps = max(0, min(8, wheel_steps))
            wheel_delta = int(_env_float("FB_WHEEL_DELTA", 420.0))
            wheel_delta = max(120, min(1600, wheel_delta))
            page.evaluate(
                """(factor) => {
                  const main = document.querySelector('div[role="main"]') || document.body;
                  if (!main) return;
                  const p = main.querySelector('[data-fbshot-primaryscroll="1"]');
                  if (p) {
                    const ch = p.clientHeight || 0;
                    const step = Math.max(450, Math.min(1600, Math.floor(ch * (factor || 0.85)) || 950));
                    try { p.scrollTop = Math.min((p.scrollHeight||0), (p.scrollTop||0) + step); } catch (e) {}
                  }
                  const sel = '[role="article"],div[role="article"],article,div[data-pagelet*="FeedUnit_"],div[data-pagelet]';
                  const els = Array.from(main.querySelectorAll(sel));
                  if (els.length > 0) {
                    const last = els[els.length - 1];
                    try { last.scrollIntoView({ block: 'end', inline: 'nearest' }); } catch (e) {}
                  }
                  if (p) {
                    const ch = p.clientHeight || 0;
                    const sh = p.scrollHeight || 0;
                    const cur = p.scrollTop || 0;
                    const step = Math.max(400, Math.min(1400, Math.floor(ch * 0.8) || 900));
                    // Wheel dispatch (some lazy loaders listen to wheel/touchpad)
                    try {
                      const evt = new WheelEvent('wheel', { deltaY: step, bubbles: true, cancelable: true });
                      p.dispatchEvent(evt);
                    } catch (e) {}
                  } else {
                    try { window.scrollBy(0, 1100); } catch (e) {}
                  }
                }"""
                ,
                scroll_factor,
            )
        except Exception:
            pass
        # Also send real wheel gestures in smaller steps (touchpad-like; reduces "stuck" cases).
        try:
            for _ in range(wheel_steps):
                page.mouse.wheel(0, wheel_delta)
                try:
                    page.wait_for_timeout(90)
                except Exception:
                    time.sleep(0.09)
        except Exception:
            pass
        try:
            page.keyboard.press("PageDown", timeout=1200)
        except Exception:
            pass

        try:
            page.wait_for_timeout(450)
        except Exception:
            time.sleep(0.45)

        _enforce_single_scroller()
        d1 = _scroll_diag()
        after_win = float(d1.get("y") or 0.0)
        after_primary = float(d1.get("primaryTop") or 0.0)
        after_primary_sh = float(d1.get("primarySH") or 0.0)
        after_primary_ch = float(d1.get("primaryCH") or 0.0)

        if has_primary:
            moved = after_primary > before_primary + 8
            # If we reached bottom of current content, wait briefly for more results to load (scrollHeight grows).
            if not moved and after_primary_sh > 0 and after_primary_ch > 0:
                remain = (after_primary_sh - after_primary_ch) - after_primary
                if remain <= 60 and after_primary_sh <= before_primary_sh + 10:
                    t0 = time.time()
                    grew = False
                    while time.time() - t0 < 12.0:
                        try:
                            page.wait_for_timeout(400)
                        except Exception:
                            time.sleep(0.4)
                        _enforce_single_scroller()
                        d2 = _scroll_diag()
                        sh2 = float(d2.get("primarySH") or 0.0)
                        if sh2 > after_primary_sh + 80:
                            grew = True
                            # After new items append, move up a bit so the thumb doesn't look "stuck at bottom"
                            # and to keep subsequent scrolling deterministic.
                            try:
                                page.evaluate(
                                    """() => {
                                      const main = document.querySelector('div[role="main"]') || document.body;
                                      const p = main ? main.querySelector('[data-fbshot-primaryscroll="1"]') : null;
                                      if (!p) return;
                                      const ch = p.clientHeight || 0;
                                      p.scrollTop = Math.max(0, (p.scrollTop||0) - Math.floor(ch * 0.35));
                                    }"""
                                )
                            except Exception:
                                pass
                            break
                    if grew:
                        return True
            # Consider progress if scrollHeight grows (new results appended) even if scrollTop didn't change much.
            if after_primary_sh > before_primary_sh + 120:
                return True
            return moved
        return after_win > before_win + 8

    # DOM capture mode:
    # - Iterate posts by aria-posinset in increasing order
    # - Scroll each post into view
    # - Expand "Xem thêm" inside the post
    # - Element screenshot (post locator), saved as post_XXX.png
    see_more_exact_class = (
        "x1i10hfl xjbqb8w x1ejq31n x18oe1m7 x1sy0etr xstzfhl x972fbf x10w94by x1qhh985 "
        "x14e42zd x9f619 x1ypdohk xt0psk2 x3ct3a4 xdj266r x14z9mp xat24cr x1lziwak xexx8yu "
        "xyri2b x18d9i69 x1c1uobl x16tdsg8 x1hl2dhg xggy1nq x1a2a7pz xkrqix3 x1sur9pj xzsf02u x1s688f"
    )

    last_saved_at = time.time()
    last_reload_at = 0.0
    last_pos = 0
    stall_rounds = 0
    stuck_same_pos_rounds = 0
    last_want_pos = None
    last_saved_count = 0

    def _scroll_to_top_results() -> None:
        """
        FB search results sometimes open with the feed already scrolled.
        Force the active scroller back to top so we don't skip early posts.
        """
        try:
            _enforce_single_scroller()
        except Exception:
            pass
        # Try multiple times because FB can fight back during hydration.
        # Use multiple strategies: JS scrollTop=0 + keyboard Home/PageUp + wheel up.
        deadline = time.time() + 14.0
        while time.time() < deadline:
            if should_cancel():
                return
            # Most "real" top gesture
            try:
                page.keyboard.press("Home", timeout=800)
            except Exception:
                pass
            try:
                # PageUp a few times in case FB uses inner scroll container.
                for _ in range(3):
                    page.keyboard.press("PageUp", timeout=800)
            except Exception:
                pass
            try:
                # Real wheel up helps trigger FB scroll handlers.
                page.mouse.wheel(0, -1800)
            except Exception:
                pass
            try:
                page.evaluate(
                    """() => {
                      const main = document.querySelector('div[role="main"]') || document.body;
                      const p = main ? main.querySelector('[data-fbshot-primaryscroll="1"]') : null;
                      try { window.scrollTo(0, 0); } catch (e) {}
                      try {
                        const se = document.scrollingElement || document.documentElement;
                        se && (se.scrollTop = 0);
                      } catch (e) {}
                      if (p) {
                        try { p.scrollTop = 0; } catch (e) {}
                      }
                    }"""
                )
            except Exception:
                pass
            try:
                page.wait_for_timeout(350)
            except Exception:
                time.sleep(0.35)
            try:
                d = _scroll_diag()
                win_y = float(d.get("y") or 0.0)
                primary_y = float(d.get("primaryTop") or 0.0)
                if win_y <= 5.0 and primary_y <= 5.0:
                    return
            except Exception:
                # If diag fails, just proceed.
                return

    def _scroll_first_result_into_view() -> None:
        """
        After forcing top, FB may still place the first result slightly below fold (sticky header/filters).
        Scroll the first real feed unit into view to avoid skipping early posts.
        """
        try:
            _enforce_single_scroller()
        except Exception:
            pass
        try:
            page.evaluate(
                """() => {
                  const main = document.querySelector('div[role="main"]') || document.body;
                  if (!main) return;
                  const vh = window.innerHeight || 800;
                  const sel = '[role="article"],div[role="article"],article,div[data-pagelet*="FeedUnit_"]';
                  const els = Array.from(main.querySelectorAll(sel));
                  if (!els || els.length === 0) return;
                  // Pick the first visible-ish unit (near top).
                  let first = null;
                  for (const el of els.slice(0, 10)) {
                    const r = el.getBoundingClientRect();
                    if (r.height < 120) continue;
                    // accept elements in upper half or slightly below
                    if (r.top < vh * 0.75) { first = el; break; }
                    if (!first) first = el;
                  }
                  if (!first) first = els[0];
                  try { first.scrollIntoView({ block: 'start', inline: 'nearest' }); } catch (e) {}
                  // Nudge up a bit to show header of the first post.
                  try { window.scrollBy(0, -120); } catch (e) {}
                  try {
                    const p = main.querySelector('[data-fbshot-primaryscroll="1"]');
                    if (p) p.scrollTop = Math.max(0, (p.scrollTop||0) - 120);
                  } catch (e) {}
                }"""
            )
        except Exception:
            pass

    def _doc_scroll_state() -> dict:
        try:
            return page.evaluate(
                """() => {
                  const se = document.scrollingElement || document.documentElement;
                  const y = window.scrollY || 0;
                  const docH = se ? (se.scrollHeight || 0) : (document.body ? (document.body.scrollHeight||0) : 0);
                  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
                  const maxY = Math.max(0, docH - vh);
                  return { y, docH, vh, maxY };
                }"""
            ) or {}
        except Exception:
            return {}

    def _wait_for_more_results(prev_doc_h: float, timeout_s: float = 12.0) -> bool:
        """
        When we're at bottom, FB may lazy-load more results only after some time.
        Wait for document height to grow.
        """
        t0 = time.time()
        pulse = 0
        while time.time() - t0 < timeout_s:
            pulse += 1
            # Trigger lazy-load using multiple "real" gestures. FB often ignores synthetic WheelEvent.
            try:
                # Prefer a real wheel input (closest to touchpad).
                page.mouse.wheel(0, 1200)
            except Exception:
                pass
            try:
                # Some layouts load more after jumping to end.
                if pulse in {2, 6, 10}:
                    page.keyboard.press("End", timeout=1200)
            except Exception:
                pass
            try:
                # Micro up/down jitter to trigger observers.
                page.keyboard.press("PageUp", timeout=900)
                page.keyboard.press("PageDown", timeout=900)
            except Exception:
                pass
            try:
                # Also attempt JS wheel dispatch on the likely container.
                page.evaluate(
                    """() => {
                      const main = document.querySelector('div[role="main"]') || document.body;
                      const vw = Math.max(document.documentElement.clientWidth, window.innerWidth||0);
                      const vh = Math.max(document.documentElement.clientHeight, window.innerHeight||0);
                      const target = (main && main.querySelector('[data-fbshot-primaryscroll="1"]')) || main || document.scrollingElement || document.documentElement;
                      if (!target) return;
                      try {
                        const evt = new WheelEvent('wheel', { deltaY: 1200, bubbles: true, cancelable: true, clientX: vw - 20, clientY: vh - 40 });
                        target.dispatchEvent(evt);
                      } catch (e) {}
                      try { window.scrollBy(0, 3); window.scrollBy(0, -3); } catch (e) {}
                    }"""
                )
            except Exception:
                pass
            try:
                page.wait_for_timeout(450)
            except Exception:
                time.sleep(0.45)
            st = _doc_scroll_state()
            try:
                dh = float(st.get("docH") or 0.0)
            except Exception:
                dh = 0.0
            if dh > prev_doc_h + 120:
                return True
        return False

    def click_see_more_exact_in_viewport() -> int:
        try:
            return int(
                page.evaluate(
                    """(cls) => {
                      const vh = window.innerHeight || document.documentElement.clientHeight || 0;
                      const vw = window.innerWidth || document.documentElement.clientWidth || 0;
                      if (vh <= 0 || vw <= 0) return 0;
                      const main = document.querySelector('div[role="main"]') || document.body;
                      if (!main) return 0;
                      const normalize = (s) => (s || '').trim().replace(/\\s+/g,' ');
                      const isInViewport = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.bottom > 40 && r.top < vh - 40 && r.right > 30 && r.left < vw - 30;
                      };
                      const nodes = Array.from(main.querySelectorAll('div')).filter(el => {
                        try {
                          if (!el) return false;
                          if (!isInViewport(el)) return false;
                          const c = normalize(el.getAttribute('class'));
                          if (c !== cls) return false;
                          const t = (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim().toLowerCase();
                          return t.startsWith('xem thêm');
                        } catch (e) { return false; }
                      });
                      let clicked = 0;
                      for (const el of nodes.slice(0, 4)) {
                        try { el.click(); clicked++; } catch (e) {}
                      }
                      return clicked;
                    }""",
                    see_more_exact_class,
                )
            )
        except Exception:
            return 0

    # Start from the very top so we don't miss early results.
    _scroll_to_top_results()
    _scroll_first_result_into_view()

    def _count_posinset_nodes() -> int:
        try:
            return int(page.locator(POST_ARIA_POSINSET_SELECTOR).count())
        except Exception:
            return 0

    def _find_post_by_pos(pos: int):
        # FB uses many elements with aria-posinset; prefer role=article first, then aria-posinset selector.
        try:
            loc = page.locator(f'{POST_ARTICLE_SELECTOR}[aria-posinset="{int(pos)}"]').first
            if loc.count() > 0:
                return loc
        except Exception:
            pass
        try:
            loc = page.locator(f'{POST_ARIA_POSINSET_SELECTOR}[aria-posinset="{int(pos)}"]').first
            if loc.count() > 0:
                return loc
        except Exception:
            pass
        return None

    def _posinset_range_hint() -> tuple[int | None, int | None]:
        """
        Return (minPos, maxPos) of aria-posinset currently present in DOM.
        Helps decide scroll direction when a desired posinset is not visible yet.
        """
        try:
            res = page.evaluate(
                """() => {
                  const nodes = Array.from(document.querySelectorAll('div[aria-posinset]')).slice(0, 2000);
                  let minP = null;
                  let maxP = null;
                  for (const el of nodes) {
                    const v = parseInt(el.getAttribute('aria-posinset') || '', 10);
                    if (!Number.isFinite(v)) continue;
                    if (minP === null || v < minP) minP = v;
                    if (maxP === null || v > maxP) maxP = v;
                  }
                  return { minP, maxP };
                }"""
            ) or {}
            mn = res.get("minP", None)
            mx = res.get("maxP", None)
            mn = int(mn) if mn is not None else None
            mx = int(mx) if mx is not None else None
            return mn, mx
        except Exception:
            return None, None

    last_mx = None
    last_mx_change_at = time.time()
    last_doc_h = None
    last_doc_h_change_at = time.time()
    last_primary_sh = None
    last_primary_sh_change_at = time.time()

    def _end_marker_present() -> bool:
        try:
            return bool(
                page.evaluate(
                    """() => {
                      try {
                        const norm = (s) => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();
                        const t = norm(document.body ? (document.body.innerText || '') : '');
                        if (!t) return false;
                        const keys = [
                          'không còn kết quả', 'khong con ket qua',
                          'không tìm thấy', 'khong tim thay',
                          'no more results', 'no results found', 'end of results'
                        ];
                        return keys.some(k => t.includes(k));
                      } catch (e) { return false; }
                    }"""
                )
            )
        except Exception:
            return False

    def _at_bottom_hint() -> bool:
        try:
            d = _scroll_diag() or {}
            p_top = float(d.get("primaryTop") or 0.0)
            p_h = float(d.get("primaryH") or 0.0)
            p_sh = float(d.get("primarySH") or 0.0)
            if p_sh > 0 and p_h > 0:
                return (p_top + p_h) >= (p_sh - 220.0)
            y = float(d.get("y") or 0.0)
            vh = float(d.get("vh") or 0.0)
            doc_h = float(d.get("docH") or 0.0)
            if doc_h > 0 and vh > 0:
                return (y + vh) >= (doc_h - 220.0)
            return False
        except Exception:
            return False

    def _track_heights() -> None:
        nonlocal last_doc_h, last_doc_h_change_at, last_primary_sh, last_primary_sh_change_at
        try:
            d = _scroll_diag() or {}
            doc_h = float(d.get("docH") or 0.0)
            p_sh = float(d.get("primarySH") or 0.0)
        except Exception:
            doc_h = 0.0
            p_sh = 0.0
        now = time.time()
        if last_doc_h is None:
            last_doc_h = doc_h
            last_doc_h_change_at = now
        elif abs(doc_h - float(last_doc_h or 0.0)) >= 40.0:
            last_doc_h = doc_h
            last_doc_h_change_at = now
        if last_primary_sh is None:
            last_primary_sh = p_sh
            last_primary_sh_change_at = now
        elif abs(p_sh - float(last_primary_sh or 0.0)) >= 40.0:
            last_primary_sh = p_sh
            last_primary_sh_change_at = now
    try:
        while True:
            _detect_checkpoint(page)
            if should_cancel():
                log("cancel", "Cancel requested.")
                break

            # Guard: if we somehow leave /search/, force back to the search URL so DOM capture stays correct.
            if expected_search_url:
                try:
                    cur = (page.url or "")
                except Exception:
                    cur = ""
                if "/search/" not in cur:
                    try:
                        log("capture", f"Navigated away from search (url={cur}). Returning to search URL…", "WARN")
                    except Exception:
                        pass
                    try:
                        page.goto(expected_search_url, wait_until="domcontentloaded", timeout=60_000)
                        _detect_checkpoint(page)
                        _wait_posts_present_or_fail()
                    except Exception as e:
                        raise ElementTimeout(f"Lost search page and failed to return: {e}")

            # Hard guard for the very first capture:
            # Some FB layouts "jump" scroll position after initial load; re-force top until we save the first image.
            if saved == 0:
                try:
                    dtop = _scroll_diag()
                    win_y0 = float(dtop.get("y") or 0.0)
                    primary_y0 = float(dtop.get("primaryTop") or 0.0)
                except Exception:
                    win_y0 = 0.0
                    primary_y0 = 0.0
                if win_y0 > 40.0 or primary_y0 > 40.0:
                    try:
                        log(
                            "capture",
                            f"Guard: forcing top before first capture (y={win_y0:.0f}, primary={primary_y0:.0f})",
                        )
                    except Exception:
                        pass
                    # Avoid aggressive top-forcing (can look like scroll up/down). Just ensure first result is visible.
                    _scroll_first_result_into_view()

            # IMPORTANT: do NOT auto reload here.
            # If we stall, end-of-results / stall logic below will decide when to stop.

            want_pos = last_pos + 1
            # If we keep trying to capture the same posinset but never manage to save,
            # we should treat it as end-of-results (FB sometimes keeps showing a non-progressing tail).
            if last_want_pos == want_pos and saved == last_saved_count:
                stuck_same_pos_rounds += 1
            else:
                stuck_same_pos_rounds = 0
            last_want_pos = want_pos
            last_saved_count = saved
            total_txt = "∞" if unlimited else str(params.max_posts)
            log("capture", f"Capturing posinset={want_pos} ({saved+1}/{total_txt})…")

            if unlimited and stuck_same_pos_rounds >= 5 and (_at_bottom_hint() or _end_marker_present()):
                try:
                    log(
                        "capture",
                        f"Stuck at posinset={want_pos} for {stuck_same_pos_rounds} rounds near end marker/bottom. Treat as end of results. Stopping.",
                        "INFO",
                    )
                except Exception:
                    pass
                break

            post = _find_post_by_pos(want_pos)
            if post is None:
                # Not rendered yet -> scroll down and wait for more results.
                _unblock_overlays(page)
                moved = _scroll_down_and_verify()
                if not moved:
                    stall_rounds += 1
                else:
                    stall_rounds = 0

                # End-of-results detection: if we already have a max posinset in DOM and we keep failing
                # to render the next one without any scrollHeight growth, stop instead of reload-looping.
                try:
                    _mn, _mx = _posinset_range_hint()
                except Exception:
                    _mn, _mx = (None, None)

                # Track max posinset changes to detect "true end" even if scrolling still reports movement.
                try:
                    if _mx is not None and (_mx != last_mx):
                        last_mx = int(_mx)
                        last_mx_change_at = time.time()
                except Exception:
                    pass

                _track_heights()

                # Strong end-of-results detection (unlimited):
                # stop only when we're beyond maxPos AND things have been stable for a while AND we're at bottom (or FB shows an end marker).
                if unlimited and _mx is not None and want_pos > int(_mx):
                    stable_for_s = min(
                        time.time() - float(last_mx_change_at or 0.0),
                        time.time() - float(last_doc_h_change_at or 0.0),
                        time.time() - float(last_primary_sh_change_at or 0.0),
                    )
                    if stable_for_s >= 22.0 and (_at_bottom_hint() or _end_marker_present()):
                        try:
                            log(
                                "capture",
                                f"Reached end of results (maxPos={_mx}, stable~{int(stable_for_s)}s, atBottom={_at_bottom_hint()}). Stopping.",
                                "INFO",
                            )
                        except Exception:
                            pass
                        break

                # Fallback: if we are past maxPos and maxPos hasn't changed for a while, stop (older heuristic).
                if unlimited and _mx is not None and want_pos > int(_mx) and (time.time() - last_mx_change_at) >= 35.0:
                    try:
                        log("capture", f"Reached stable end of results (maxPos={_mx}, stable>=35s). Stopping.", "INFO")
                    except Exception:
                        pass
                    break
                if _mx is not None and want_pos > int(_mx) and stall_rounds >= 8:
                    try:
                        log("capture", f"Reached end of results (maxPos={_mx}). Stopping capture loop.", "INFO")
                    except Exception:
                        pass
                    break

                # Give FB time to lazy-load more results before forcing reload.
                try:
                    st = _doc_scroll_state()
                    prev_h = float(st.get("docH") or 0.0)
                except Exception:
                    prev_h = 0.0
                if stall_rounds >= 6:
                    try:
                        grew = _wait_for_more_results(prev_h, timeout_s=10.0)
                    except Exception:
                        grew = False
                    if grew:
                        stall_rounds = 0
                        continue

                # IMPORTANT: do NOT auto reload. User requested no auto-reload behavior.
                # If we truly reach the end, the end-of-results detection above will stop the loop.
                continue

            # Ensure post is in view (DOM-safe) before screenshot.
            try:
                made_visible = False
                for _try in range(10):
                    _unblock_overlays(page)
                    _enforce_single_scroller()

                    # Re-find if detached/virtualized
                    try:
                        if post is None or post.count() <= 0:
                            post = _find_post_by_pos(want_pos)
                    except Exception:
                        post = _find_post_by_pos(want_pos)
                    if post is None:
                        break

                    try:
                        page.evaluate(
                            """(el) => { try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {} }""",
                            post,
                        )
                    except Exception:
                        pass

                    try:
                        page.mouse.wheel(0, 450)
                    except Exception:
                        pass
                    try:
                        page.wait_for_timeout(180)
                    except Exception:
                        time.sleep(0.18)

                    try:
                        if post.is_visible(timeout=600):
                            made_visible = True
                            break
                    except Exception:
                        continue

                    mn, mx = _posinset_range_hint()
                    if mx is not None and want_pos > mx:
                        _scroll_down_and_verify()
                    elif mn is not None and want_pos < mn:
                        try:
                            page.mouse.wheel(0, -800)
                        except Exception:
                            pass
                    else:
                        _scroll_down_and_verify()

                if not made_visible:
                    raise ElementTimeout(
                        f"scroll_into_view timeout for posinset={want_pos}: element not visible after retries"
                    )
            except PWTimeoutError as e:
                raise ElementTimeout(f"scroll_into_view timeout for posinset={want_pos}: {e}")

            try:
                _apply_screenshot_overrides(page)
                _focus_and_dismiss_overlays(page)
                _unblock_overlays(page)
            except Exception:
                pass

            # Expand see more within the post (DOM-based).
            # IMPORTANT: best-effort only.
            # Even if expanding fails, still take a screenshot (better to save partial content than skip the post).
            try:
                _expand_see_more(post, page)
            except ElementTimeout as e:
                try:
                    left = 0
                    try:
                        left = int(post.evaluate("""(el) => {
                          try {
                            if (!el) return 0;
                            const norm = (s) => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();
                            const msg = el.querySelector('div[data-ad-preview="message"]') || el;
                            const nodes = Array.from(msg.querySelectorAll('div[role="button"][tabindex="0"],div[role="button"]')).slice(0, 600);
                            let c = 0;
                            for (const n of nodes) {
                              const t = norm(n.innerText || n.textContent || '');
                              if (t === 'xem thêm' || t === 'see more' || t.startsWith('xem thêm') || t.startsWith('see more')) c++;
                            }
                            return c;
                          } catch (e) { return 0; }
                        }""") or 0)
                    except Exception:
                        left = 0
                    log(
                        "capture",
                        f'Expand "Xem thêm" failed; will still screenshot. posinset={want_pos} see_more_candidates={left} err={e}',
                        "WARN",
                    )
                except Exception:
                    pass
                try:
                    _unblock_overlays(page)
                except Exception:
                    pass
                # Retry once quickly (FB can hydrate the button late)
                try:
                    _expand_see_more(post, page)
                except Exception as e2:
                    log(
                        "capture",
                        f'Expand "Xem thêm" still failing; will screenshot anyway. posinset={want_pos} err={e2}',
                        "WARN",
                    )
            except Exception as e:
                log("capture", f'Expand "Xem thêm" error; will screenshot anyway. posinset={want_pos} err={e}', "WARN")

            # Wait a bit for media/layout; keep it short to preserve speed.
            try:
                _wait_post_not_loading(page, post, timeout_ms=4000)
                _wait_post_media_ready(page, post, timeout_ms=6000)
            except Exception:
                pass

            final_path = out_dir / f"post_{next_index:03d}.png"
            # HTML saving removed by user request: only save .png screenshots.
            try:
                post.screenshot(path=str(final_path), timeout=90_000)
            except Exception as e:
                msg = str(e).lower()
                if (
                    "has been closed" in msg
                    or "target page" in msg
                    or "browser has been closed" in msg
                    or "connection closed" in msg
                ):
                    raise
                log("capture", f"Post screenshot error (will retry): posinset={want_pos} err={e}", "WARN")
                continue

            saved += 1
            next_index += 1
            last_pos = want_pos
            last_saved_at = time.time()
            progress(saved, -1 if unlimited else params.max_posts)
            log("capture", f"Saved {final_path.name} (posinset={want_pos})")
            _sleep_action(params, log=log, reason="after capture")
            if not unlimited and saved >= int(params.max_posts):
                break
    finally:
        _uninstall_photo_nav_guard()

    if not unlimited and saved < params.max_posts:
        log("capture", f"Stopped early: saved={saved}")
    if unlimited:
        try:
            log("capture", f"Capture loop finished (unlimited). Saved={saved}")
        except Exception:
            pass
    if saved <= 0:
        try:
            log("capture", "Saved=0. This usually means results didn't render or were blocked.", "WARN")
        except Exception:
            pass
    return saved

