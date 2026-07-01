"""미블(mrblog.net) 네이버 로그인 세션 관리 - 1회 로그인 후 자동 갱신.

브라우저 프로필을 저장(PROFILE)해서 네이버 세션을 유지한다.

  python tools/mrblog_auth.py login     # 최초 1회: 창이 뜨면 네이버로 '로그인 유지' 체크하고 로그인
  python tools/mrblog_auth.py refresh    # 이후 자동(스케줄러): 저장된 세션으로 쿠키만 갱신, 재로그인 X

둘 다 성공 시 .env 의 MRBLOG_COOKIE 갱신 + 앱 컨테이너 재시작.
refresh 가 실패(세션 만료)하면 exit 2 → 그때만 login 을 한 번 더.
"""
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "https://www.mrblog.net"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
PROFILE = os.path.join(ROOT, "tools", ".mrblog_profile")   # 네이버 세션 저장(gitignore 필수)


def _write_env(cookie: str) -> None:
    lines = open(ENV_PATH, encoding="utf-8").read().splitlines() if os.path.exists(ENV_PATH) else []
    out, found = [], False
    for ln in lines:
        if ln.startswith("MRBLOG_COOKIE="):
            out.append(f'MRBLOG_COOKIE="{cookie}"'); found = True
        else:
            out.append(ln)
    if not found:
        out.append(f'MRBLOG_COOKIE="{cookie}"')
    open(ENV_PATH, "w", encoding="utf-8").write("\n".join(out) + "\n")


def _logged_in(ctx) -> bool:
    """새 탭으로 /campaigns 확인 → /login 으로 안 튕기면 로그인 상태."""
    pg = ctx.new_page()
    try:
        pg.goto(BASE + "/campaigns", wait_until="domcontentloaded", timeout=15000)
        return "/login" not in pg.url
    except Exception:
        return False
    finally:
        pg.close()


def _save_and_restart(ctx) -> int:
    cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies(BASE))
    if not cookie:
        print("쿠키 추출 실패"); return 1
    _write_env(cookie)
    print(f"✅ MRBLOG_COOKIE 갱신 ({len(cookie)}자)")
    try:
        subprocess.run(["docker", "compose", "up", "-d", "app"], cwd=ROOT, timeout=180)
        print("✅ 앱 재시작 완료")
    except Exception as e:
        print(f"(앱 재시작 수동 필요: docker compose up -d app) — {e}")
    return 0


def run(mode: str) -> int:
    os.makedirs(PROFILE, exist_ok=True)
    headless = (mode == "refresh")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if mode == "login":
                page.goto(BASE + "/login", wait_until="domcontentloaded")
                print("=" * 56)
                print("  열린 창에서 '네이버'로 로그인하세요('로그인 유지' 체크!).")
                print("  로그인되면 자동 저장합니다 (최대 5분).")
                print("=" * 56, flush=True)
                deadline = time.time() + 300
                while time.time() < deadline:
                    time.sleep(3)
                    if _logged_in(ctx):
                        return _save_and_restart(ctx)
                print("시간 초과 — 다시 시도해주세요."); return 2
            else:  # refresh: 저장된 네이버 세션으로 OAuth 자동 재승인
                page.goto(BASE + "/login/naver", wait_until="domcontentloaded", timeout=30000)
                deadline = time.time() + 40
                while time.time() < deadline:
                    if _logged_in(ctx):
                        return _save_and_restart(ctx)
                    time.sleep(3)
                print("세션 만료로 자동 갱신 실패 → 'login' 을 한 번 더 실행하세요.")
                return 2
        finally:
            ctx.close()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "refresh"
    if mode not in ("login", "refresh"):
        print("usage: python tools/mrblog_auth.py [login|refresh]"); sys.exit(64)
    sys.exit(run(mode))
