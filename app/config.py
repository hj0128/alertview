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
# 운영 알림(미블 쿠키 만료 등)을 받을 관리자 텔레그램 chat_id. 비우면 알림 안 보냄.
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

# --- 수집 ---
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
DEMO = os.getenv("DEMO", "0").strip() in ("1", "true", "True")

# --- DB ---
# DATABASE_URL 이 있으면 PostgreSQL(운영/도커), 없으면 SQLite 파일(DB_PATH)로 폴백.
# 예) postgresql://alertview:alertview@db:5432/alertview
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
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

# 미블: 목록 전체는 로그인 필요(네이버 로그인). 로그인된 브라우저의 쿠키 헤더를 그대로 넣으면
# 전체 목록을 수집한다. 비우면 공개 홈 노출분(약 30건)만 수집.
# 값은 브라우저 개발자도구 > Network > 요청 헤더의 Cookie 문자열 전체를 그대로 붙여넣기.
MRBLOG_COOKIE = os.getenv("MRBLOG_COOKIE", "").strip()

# 지역 정규화: 내장 사전으로 못 잡는 역/랜드마크(예: 신용산역)를 카카오 로컬 API로 보정.
# 비우면 내장 사전만 사용(미해결은 시/도까지만 또는 미표시). 무료 키: developers.kakao.com
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()

# 웹 피드: 무한스크롤 1회 로드 개수, 필터링 시 훑을 최대 행 수
FEED_PAGE = int(os.getenv("FEED_PAGE", "60"))
FEED_SCAN_MAX = int(os.getenv("FEED_SCAN_MAX", "20000"))
