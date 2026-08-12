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
# 세션 쿠키에 Secure 플래그를 붙일지. HTTPS 로 서비스하면 1(기본).
# 순수 HTTP 로 로컬 테스트할 때만 0 으로 두면 로그인 세션이 유지된다.
WEB_COOKIE_SECURE = os.getenv("WEB_COOKIE_SECURE", "1").strip() in ("1", "true", "True")

# --- 로컬 테스트용 개발 로그인 (텔레그램 로그인 건너뜀). 운영에선 0/미설정 ---
WEB_DEV_LOGIN = os.getenv("WEB_DEV_LOGIN", "0").strip() in ("1", "true", "True")
WEB_DEV_CHATID = os.getenv("WEB_DEV_CHATID", "").strip()

# 디너의여왕 수집 최대 페이지 수 (1페이지 약 30건)
DQ_MAX_PAGES = int(os.getenv("DQ_MAX_PAGES", "0"))  # 0 = 끝까지(자동 종료)

# 미블: 목록 전체는 로그인 필요(네이버 로그인). 로그인된 브라우저의 쿠키 헤더를 그대로 넣으면
# 전체 목록을 수집한다. 비우면 공개 홈 노출분(약 30건)만 수집.
# 값은 브라우저 개발자도구 > Network > 요청 헤더의 Cookie 문자열 전체를 그대로 붙여넣기.
MRBLOG_COOKIE = os.getenv("MRBLOG_COOKIE", "").strip()

# 레뷰(revu.net): 캠페인 목록 API(api.weble.net)가 로그인 토큰을 요구.
# 계정(이메일/비번)으로 로그인해 JWT 토큰을 발급받아 수집한다(토큰 수명 약 15일, 자동 재로그인).
# 비우면 레뷰 어댑터는 비활성. 알림 전용 부계정 사용을 권장.
REVU_USERNAME = os.getenv("REVU_USERNAME", "").strip()
REVU_PASSWORD = os.getenv("REVU_PASSWORD", "").strip()
REVU_MAX_PAGES = int(os.getenv("REVU_MAX_PAGES", "0"))  # 0 = total 만큼 끝까지

# 체험뷰(chvu.co.kr): /v2/campaigns JSON API. 캠페인이 매우 많아 매 5분 수집은 과해서
# 별도 주기(기본 30분)로만 수집. 0 이면 매 폴링.
CHVU_MIN_INTERVAL = int(os.getenv("CHVU_MIN_INTERVAL", "1800"))

# 지역 정규화: 내장 사전으로 못 잡는 역/랜드마크(예: 신용산역)를 카카오 로컬 API로 보정.
# 비우면 내장 사전만 사용(미해결은 시/도까지만 또는 미표시). 무료 키: developers.kakao.com
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()

# 지도(웹) 표시용 카카오맵 JavaScript 키. REST 키와 별개(같은 카카오 앱의 'JavaScript 키').
# 카카오 developers > 앱 > 앱 키 > JavaScript 키. 그리고 앱 > 플랫폼 > Web 에 도메인 등록 필요.
# 비우면 지도 배경은 기존(영어) 대체 지도로 표시.
KAKAO_JS_KEY = os.getenv("KAKAO_JS_KEY", "").strip()

# 웹 피드: 무한스크롤 1회 로드 개수, 필터링 시 훑을 최대 행 수
FEED_PAGE = int(os.getenv("FEED_PAGE", "60"))
FEED_SCAN_MAX = int(os.getenv("FEED_SCAN_MAX", "20000"))

# 종료(마감) 캠페인 정리: 피드에선 마감일 지나면 즉시 숨기고, 마감 후 이 일수가
# 지나면 DB 에서 삭제(유예). 0 이면 삭제 안 함(숨기기만).
PURGE_GRACE_DAYS = int(os.getenv("PURGE_GRACE_DAYS", "3"))

# 방해금지(밤) 시간대: 이 시간엔 텔레그램 알림을 보내지 않고 대기열에 쌓았다가 종료 후 발송.
# QUIET_START~QUIET_END (24h, 로컬시각=KST). 자정을 넘는 구간 지원(예: 23~8). START==END 이면 비활성.
QUIET_START = int(os.getenv("QUIET_START", "23"))
QUIET_END = int(os.getenv("QUIET_END", "8"))
