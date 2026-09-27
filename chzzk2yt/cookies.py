"""네이버 쿠키(NID_AUT/NID_SES) 자동 갱신.

방식: Playwright 전용 브라우저 프로필(data/naver_profile)에 한 번 로그인해 두면
실행할 때마다 그 프로필로 chzzk.naver.com 에 접속해 세션을 갱신하고 쿠키를 읽어온다.
사용자의 실제 Edge/Chrome 프로필은 건드리지 않는다.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

from .chzzk import UA

log = logging.getLogger("chzzk2yt.cookies")
NAMES = ("NID_AUT", "NID_SES")
STATUS_URL = "https://comm-api.game.naver.com/nng_main/v1/user/getUserStatus"
LOGIN_URL = "https://nid.naver.com/nidlogin.login?url=https%3A%2F%2Fchzzk.naver.com%2F"


def check(cookies: dict) -> tuple[bool, str | None]:
    """쿠키가 로그인 상태인지 치지직에 직접 확인. (loggedIn, nickname)"""
    if not cookies.get("NID_AUT") or not cookies.get("NID_SES"):
        return False, None
    try:
        r = requests.get(STATUS_URL, cookies=cookies, headers={"User-Agent": UA}, timeout=15)
        c = r.json().get("content") or {}
        return bool(c.get("loggedIn")), c.get("nickname")
    except Exception as e:
        log.warning("쿠키 확인 실패: %r", e)
        return False, None


def _launch(p, profile: Path, channel: str, headless: bool):
    kw = dict(user_data_dir=str(profile), headless=headless, user_agent=UA,
              args=["--disable-blink-features=AutomationControlled"])
    if channel and channel != "chromium":
        kw["channel"] = channel
    return p.chromium.launch_persistent_context(**kw)


def _read(ctx) -> dict:
    return {c["name"]: c["value"] for c in ctx.cookies(["https://naver.com", "https://chzzk.naver.com"])
            if c["name"] in NAMES}


# ── 전체 네이버 쿠키 저장/복원 ─────────────────────────────
# 브라우저 프로필의 쿠키 DB에만 의존하면 (1) 세션 쿠키는 브라우저 종료 시 삭제되고
# (2) 로그인 직후 바로 닫으면 디스크에 기록되기 전에 종료될 수 있다.
# 그래서 로그인에 쓰인 네이버 쿠키 전체를 직접 파일로 저장하고, 갱신 시 주입한다.
def _state_path(data_dir: Path) -> Path:
    return data_dir / "naver_cookies.json"


def _dump_state(ctx, data_dir: Path):
    all_ck = [c for c in ctx.cookies() if c.get("domain", "").endswith("naver.com")]
    _state_path(data_dir).write_text(json.dumps(all_ck, ensure_ascii=False), encoding="utf-8")


def _load_state(data_dir: Path) -> list:
    try:
        return json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
    except Exception:
        return []


def interactive_login(data_dir: Path, channel: str) -> dict:
    """브라우저 창을 띄워 사용자가 직접 로그인 (최초 1회, 만료 시 재실행)."""
    from playwright.sync_api import sync_playwright

    profile = data_dir / "naver_profile"
    print("브라우저가 열리면 네이버에 로그인하세요. ('로그인 상태 유지' 체크 권장)\n최대 5분 대기합니다.")
    with sync_playwright() as p:
        ctx = _launch(p, profile, channel, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL)
        deadline = time.time() + 300
        cookies = {}
        while time.time() < deadline:
            time.sleep(3)
            cookies = _read(ctx)
            if all(cookies.get(n) for n in NAMES) and check(cookies)[0]:
                break
        # 로그인 후 치지직 페이지까지 한 번 거쳐 쿠키를 모두 받은 뒤 저장
        try:
            page.goto("https://chzzk.naver.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
        except Exception:
            pass
        cookies = _read(ctx)
        if check(cookies)[0]:
            _dump_state(ctx, data_dir)
        ctx.close()
    ok, nick = check(cookies)
    if not ok:
        raise SystemExit("로그인을 확인하지 못했습니다. 다시 시도하세요.")
    _save_cache(data_dir, cookies)
    print(f"로그인 확인: {nick}  (쿠키 저장: {_state_path(data_dir)})")
    return cookies


def refresh_from_browser(data_dir: Path, channel: str, headless: bool) -> dict:
    """저장된 쿠키를 브라우저에 주입 → 치지직 접속으로 세션 연장 → 갱신된 쿠키 저장."""
    from playwright.sync_api import sync_playwright

    state = _load_state(data_dir)
    if not state:
        raise RuntimeError("저장된 네이버 로그인이 없습니다 — 프로그램에서 '네이버 로그인'을 누르세요.")
    with sync_playwright() as p:
        ctx = _launch(p, data_dir / "naver_profile", channel, headless)
        ctx.add_cookies([{k: v for k, v in c.items() if k in
                          ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")}
                         for c in state])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://chzzk.naver.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)  # 세션 갱신(Set-Cookie) 대기
        cookies = _read(ctx)
        if check(cookies)[0]:
            _dump_state(ctx, data_dir)
        ctx.close()
    return cookies


def _cache_path(data_dir: Path) -> Path:
    return data_dir / "cookies.json"


def _save_cache(data_dir: Path, cookies: dict):
    _cache_path(data_dir).write_text(
        json.dumps({**cookies, "saved_at": int(time.time())}), encoding="utf-8"
    )


def _load_cache(data_dir: Path) -> dict:
    try:
        d = json.loads(_cache_path(data_dir).read_text(encoding="utf-8"))
        return {n: d.get(n, "") for n in NAMES}
    except Exception:
        return {}


def get_cookies(cfg, notify=None) -> dict:
    """설정 모드에 따라 유효한 쿠키를 반환. 실패하면 빈 dict (공개 영상은 계속 가능)."""
    c = cfg["cookies"]
    mode = c["mode"]
    if mode == "none":
        return {}
    if mode == "manual":
        cookies = {n: c.get(n, "") for n in NAMES}
        ok, nick = check(cookies)
    else:
        cookies, ok, nick = {}, False, None
        if not (cfg.data_dir / "naver_profile").exists() and not _load_cache(cfg.data_dir):
            log.info("네이버 로그인 전 — 공개 영상만 받습니다 (19+ 영상은 '네이버 로그인' 후 진행)")
            return {}
        try:
            cookies = refresh_from_browser(cfg.data_dir, c["browser_channel"], c["headless"])
            ok, nick = check(cookies)
        except Exception as e:
            log.warning("브라우저 쿠키 갱신 실패: %r", e)
        if not ok:  # 브라우저 갱신이 안 돼도 마지막으로 저장된 쿠키가 살아 있으면 사용
            cached = _load_cache(cfg.data_dir)
            ok, nick = check(cached)
            if ok:
                log.warning("브라우저 갱신 실패 → 저장된 쿠키 사용")
                cookies = cached
    if ok:
        log.info("네이버 쿠키 유효 (%s)", nick)
        if mode == "browser":
            _save_cache(cfg.data_dir, cookies)
        return cookies

    msg = "네이버 로그인이 만료됐습니다 — 프로그램에서 '네이버 로그인'을 다시 누르세요 (공개 영상만 진행)"
    if mode == "manual":
        msg = "config.toml 의 NID_AUT/NID_SES 가 만료됐습니다. (공개 영상만 진행)"
    log.error(msg)
    if notify:
        notify(msg, once_per_day="cookie_expired")
    return {}
