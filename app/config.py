"""환경 변수 설정 로더."""
from __future__ import annotations
import os
from dotenv import load_dotenv

try:
    load_dotenv(encoding="utf-8-sig")
except Exception:
    try:
        load_dotenv(encoding="latin-1")
    except Exception:
        pass

# --- 텔레그램 ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")

# --- 수집 ---
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
DEMO = os.getenv("DEMO", "0").strip() in ("1", "true", "True")
DB_PATH = os.getenv("DB_PATH", "data.db")
SEND_GAP = float(os.getenv("SEND_GAP", "0.05"))

# --- 웹 ---
WEB_ENABLED = os.getenv("WEB_ENABLED", "1").strip() in ("1", "true", "True")
WEB_PORT = int(os.getenv("WEB_PORT", os.getenv("PORT", "8000")))
WEB_SECRET = os.getenv("WEB_SECRET", "change-me-please-" + (BOT_TOKEN[-8:] or "dev"))

# --- 로컬 테스트용 개발 로그인 (텔레그램 로그인 건너뜀). 운영에선 0/미설정 ---
WEB_DEV_LOGIN = os.getenv("WEB_DEV_LOGIN", "0").strip() in ("1", "true", "True")
WEB_DEV_CHATID = os.getenv("WEB_DEV_CHATID", "").strip()

# 디너의여왕 수집 최대 페이지 수 (1페이지 약 30건)
DQ_MAX_PAGES = int(os.getenv("DQ_MAX_PAGES", "0"))  # 0 = 끝까지(자동 종료)
