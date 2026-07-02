"""저장소: 사용자 / 필터 / 수집 캠페인(seen) / 본 항목(viewed).

백엔드는 두 가지를 지원한다(같은 함수 API).
  - PostgreSQL: 환경변수 DATABASE_URL 이 있으면 psycopg 로 연결(운영/도커).
  - SQLite    : DATABASE_URL 이 없으면 파일 DB(로컬 개발/테스트 폴백).

SQL 은 양쪽 모두에서 동작하도록 통일했다.
  - 플레이스홀더는 '?' 로 작성하고, PostgreSQL 일 때만 '%s' 로 치환(_q).
  - UPSERT 는 'ON CONFLICT ... DO NOTHING / DO UPDATE' (둘 다 지원).
  - 행 접근은 항상 컬럼명(r["col"]) 으로 한다(dict_row / sqlite3.Row 공통).
"""
from __future__ import annotations

import datetime
import logging
import sqlite3
import threading
import time
from typing import List, Optional, Tuple

from . import config
from .region_norm import normalize_offline, region_from_title

log = logging.getLogger(__name__)

_lock = threading.Lock()
_conn = None                      # sqlite3.Connection | psycopg.Connection
_backend: str = "sqlite"          # "postgres" | "sqlite"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _is_pg() -> bool:
    return _backend == "postgres"


def _q(sql: str) -> str:
    """플레이스홀더 변환: PostgreSQL 은 '%s', SQLite 는 '?'."""
    return sql.replace("?", "%s") if _is_pg() else sql


def _tiebreak() -> str:
    """list_recent 정렬 보조키(삽입 순서). PG=id(serial), SQLite=rowid."""
    return "id" if _is_pg() else "rowid"


# --------------------------------------------------------------------------- #
# 초기화
# --------------------------------------------------------------------------- #
def init(db_path: Optional[str] = None) -> None:
    """DATABASE_URL 이 있으면 PostgreSQL, 없으면 SQLite(db_path 또는 config.DB_PATH)."""
    global _conn, _backend
    if getattr(config, "DATABASE_URL", ""):
        _backend = "postgres"
        _connect_pg(config.DATABASE_URL)
    else:
        _backend = "sqlite"
        _connect_sqlite(db_path or config.DB_PATH)
    _create_schema()
    _migrate()
    _conn.commit()
    log.info("DB 초기화 완료 (backend=%s)", _backend)


def _connect_sqlite(path: str) -> None:
    global _conn
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL;")


def _connect_pg(dsn: str) -> None:
    """psycopg3 연결(컨테이너 기동 직후를 대비해 재시도)."""
    global _conn
    import psycopg
    from psycopg.rows import dict_row

    last_err = None
    for attempt in range(1, 31):  # 최대 ~30초 대기(db 컨테이너 healthcheck 대비)
        try:
            _conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
            return
        except Exception as e:  # OperationalError 등
            last_err = e
            log.warning("PostgreSQL 연결 재시도 %d/30: %s", attempt, e)
            time.sleep(1)
    raise RuntimeError(f"PostgreSQL 연결 실패: {last_err}")


def _create_schema() -> None:
    int_pk = "BIGINT" if _is_pg() else "INTEGER"
    # seen 테이블의 삽입순서 보조키: PG 는 serial 컬럼, SQLite 는 내장 rowid 사용
    seen_seq = "id BIGSERIAL," if _is_pg() else ""
    stmts = [
        f"""
        CREATE TABLE IF NOT EXISTS users (
            chat_id    {int_pk} PRIMARY KEY,
            active     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            seen_until TEXT
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS filters (
            chat_id {int_pk} NOT NULL,
            ftype   TEXT NOT NULL,
            value   TEXT NOT NULL,
            UNIQUE(chat_id, ftype, value)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS seen (
            site TEXT NOT NULL, cid TEXT NOT NULL,
            title TEXT, url TEXT,
            region TEXT, category TEXT, channel TEXT,
            dday INTEGER, applicants INTEGER, recruit INTEGER, competition REAL, image TEXT,
            deadline TEXT,
            first_seen TEXT,
            {seen_seq}
            PRIMARY KEY(site, cid)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS viewed (
            chat_id {int_pk} NOT NULL, site TEXT NOT NULL, cid TEXT NOT NULL,
            PRIMARY KEY(chat_id, site, cid)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS favorites (
            chat_id {int_pk} NOT NULL, site TEXT NOT NULL, cid TEXT NOT NULL,
            PRIMARY KEY(chat_id, site, cid)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS reminders (
            chat_id {int_pk} NOT NULL, site TEXT NOT NULL, cid TEXT NOT NULL,
            PRIMARY KEY(chat_id, site, cid)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS presets (
            chat_id {int_pk} NOT NULL, name TEXT NOT NULL,
            payload TEXT, created_at TEXT,
            PRIMARY KEY(chat_id, name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS region_cache (
            raw TEXT PRIMARY KEY, norm TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY, value TEXT
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS visits (
            {"id BIGSERIAL PRIMARY KEY" if _is_pg() else "id INTEGER PRIMARY KEY AUTOINCREMENT"},
            ts TEXT, ip TEXT, path TEXT, referrer TEXT, ua TEXT, uid {int_pk}
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS adapter_health (
            key TEXT PRIMARY KEY, ts TEXT, cnt INTEGER, status TEXT, note TEXT
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS inquiries (
            {"id BIGSERIAL PRIMARY KEY" if _is_pg() else "id INTEGER PRIMARY KEY AUTOINCREMENT"},
            ts TEXT, uid {int_pk}, contact TEXT, message TEXT, handled INTEGER DEFAULT 0
        )
        """,
    ]
    for s in stmts:
        _conn.execute(s)


def _migrate() -> None:
    """기존 DB에 누락된 컬럼을 추가(하위호환)."""
    add = [
        ("users", "seen_until", "seen_until TEXT"),
        ("seen", "region", "region TEXT"), ("seen", "category", "category TEXT"),
        ("seen", "channel", "channel TEXT"), ("seen", "dday", "dday INTEGER"),
        ("seen", "applicants", "applicants INTEGER"), ("seen", "recruit", "recruit INTEGER"),
        ("seen", "competition", "competition REAL"),
        ("seen", "image", "image TEXT"),
        ("seen", "deadline", "deadline TEXT"),   # 마감 절대 날짜(YYYY-MM-DD) → D-day 동적 계산용
        ("seen", "region_raw", "region_raw TEXT"),  # 사이트 원본 지역 표기(정규화 전)
    ]
    if _is_pg():
        for table, _col, decl in add:
            # PostgreSQL 은 IF NOT EXISTS 지원 → 안전하게 반복 가능
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {decl}")
    else:
        def cols(t):
            return {r[1] for r in _conn.execute(f"PRAGMA table_info({t})")}
        for table, col, decl in add:
            if col not in cols(table):
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")
    # 기존 사용자: seen_until 없으면 지금 시점으로 백필(과거 캠페인 NEW 폭탄 방지)
    _conn.execute(_q("UPDATE users SET seen_until=? WHERE seen_until IS NULL"), (_now(),))
    # 기존 캠페인: 저장된 dday로 마감일(deadline) 1회 백필 → 재수집 없이도 D-day 동적 계산
    if _is_pg():
        _conn.execute("UPDATE seen SET deadline=(first_seen::date + (dday || ' days')::interval)::text "
                      "WHERE deadline IS NULL AND dday IS NOT NULL")
    else:
        _conn.execute("UPDATE seen SET deadline=date(first_seen, '+' || dday || ' days') "
                      "WHERE deadline IS NULL AND dday IS NOT NULL")
    _conn.execute("UPDATE seen SET region_raw=region WHERE region_raw IS NULL")
    # '기타'로 잘못 잡힌 행은 제목에서 위치를 다시 추출해 교정(예: 성수→서울 성동구,
    # '[기자단] ..성수점' 같은 가게이름은 지역 없음으로). 교정되면 '기타'에서 빠져 재처리 안 됨.
    try:
        rows = _conn.execute("SELECT site, cid, title FROM seen WHERE region='기타'").fetchall()
        for r in rows:
            cand = region_from_title(r["title"] or "")
            region = normalize_offline(cand) if cand else ""
            _conn.execute(_q("UPDATE seen SET region=?, region_raw=? WHERE site=? AND cid=?"),
                          (region, cand, r["site"], r["cid"]))
    except Exception as e:
        log.warning("'기타' 지역 재산출 마이그레이션 건너뜀: %s", e)


def _c():
    assert _conn is not None, "db.init() 를 먼저 호출하세요"
    return _conn


# --------------------------------------------------------------------------- #
# 사용자
# --------------------------------------------------------------------------- #
def upsert_user(chat_id: int) -> None:
    with _lock:
        _c().execute(_q(
            "INSERT INTO users(chat_id, active, created_at, seen_until) VALUES(?,1,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET active=1"),
            (chat_id, _now(), _now()),
        )
        _c().commit()


def set_active(chat_id: int, active: bool) -> None:
    with _lock:
        _c().execute(_q("UPDATE users SET active=? WHERE chat_id=?"),
                     (1 if active else 0, chat_id))
        _c().commit()


def active_users() -> List[int]:
    with _lock:
        rows = _c().execute("SELECT chat_id FROM users WHERE active=1").fetchall()
    return [r["chat_id"] for r in rows]


def is_active(chat_id: int) -> bool:
    with _lock:
        row = _c().execute(_q("SELECT active FROM users WHERE chat_id=?"),
                           (chat_id,)).fetchone()
    return bool(row and row["active"])


def get_seen_until(chat_id: int) -> str:
    with _lock:
        row = _c().execute(_q("SELECT seen_until FROM users WHERE chat_id=?"),
                           (chat_id,)).fetchone()
    return (row["seen_until"] if row and row["seen_until"] else "0000-00-00 00:00:00")


def mark_all_seen(chat_id: int) -> None:
    """'모두 확인': 현재 시점까지 모두 본 것으로. 개별 viewed 기록은 정리.
    사용자 행이 없어도 확실히 반영되도록 upsert 한다."""
    with _lock:
        _c().execute(_q(
            "INSERT INTO users(chat_id, active, created_at, seen_until) VALUES(?,1,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET seen_until=excluded.seen_until"),
            (chat_id, _now(), _now()),
        )
        _c().execute(_q("DELETE FROM viewed WHERE chat_id=?"), (chat_id,))
        _c().commit()


# --------------------------------------------------------------------------- #
# 본 항목(개별)
# --------------------------------------------------------------------------- #
def mark_viewed(chat_id: int, site: str, cid: str) -> None:
    with _lock:
        _c().execute(_q(
            "INSERT INTO viewed(chat_id, site, cid) VALUES(?,?,?) ON CONFLICT DO NOTHING"),
            (chat_id, site, cid),
        )
        _c().commit()


def mark_viewed_many(chat_id: int, pairs) -> None:
    """여러 캠페인을 한 번에 '읽음'으로 표시('모두 읽음')."""
    pairs = list(pairs)
    if not pairs:
        return
    with _lock:
        cur = _c().cursor()
        cur.executemany(
            _q("INSERT INTO viewed(chat_id, site, cid) VALUES(?,?,?) ON CONFLICT DO NOTHING"),
            [(chat_id, s, c) for (s, c) in pairs],
        )
        _c().commit()
        cur.close()


def viewed_set(chat_id: int) -> set:
    with _lock:
        rows = _c().execute(_q("SELECT site, cid FROM viewed WHERE chat_id=?"),
                            (chat_id,)).fetchall()
    return {(r["site"], r["cid"]) for r in rows}


# --------------------------------------------------------------------------- #
# 찜(즐겨찾기)
# --------------------------------------------------------------------------- #
def set_favorite(chat_id: int, site: str, cid: str, on: bool) -> None:
    with _lock:
        if on:
            _c().execute(_q("INSERT INTO favorites(chat_id, site, cid) VALUES(?,?,?) "
                            "ON CONFLICT DO NOTHING"), (chat_id, site, cid))
        else:
            _c().execute(_q("DELETE FROM favorites WHERE chat_id=? AND site=? AND cid=?"),
                         (chat_id, site, cid))
        _c().commit()


def favorites_set(chat_id: int) -> set:
    with _lock:
        rows = _c().execute(_q("SELECT site, cid FROM favorites WHERE chat_id=?"),
                            (chat_id,)).fetchall()
    return {(r["site"], r["cid"]) for r in rows}


def save_preset(chat_id: int, name: str, payload: str) -> None:
    with _lock:
        _c().execute(_q(
            "INSERT INTO presets(chat_id, name, payload, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id, name) DO UPDATE SET payload=excluded.payload"),
            (chat_id, name, payload, _now()))
        _c().commit()


def list_presets(chat_id: int) -> List[dict]:
    with _lock:
        rows = _c().execute(_q(
            "SELECT name, payload FROM presets WHERE chat_id=? ORDER BY created_at"),
            (chat_id,)).fetchall()
    return [{"name": r["name"], "payload": r["payload"]} for r in rows]


def delete_preset(chat_id: int, name: str) -> None:
    with _lock:
        _c().execute(_q("DELETE FROM presets WHERE chat_id=? AND name=?"), (chat_id, name))
        _c().commit()


def favorites_rows(chat_id: int) -> List[dict]:
    """사용자가 찜한 캠페인의 seen 행(최신순). 마감 지난 것도 본인이 담았으니 노출."""
    with _lock:
        rows = _c().execute(_q(
            "SELECT s.* FROM seen s JOIN favorites f ON f.site=s.site AND f.cid=s.cid "
            f"WHERE f.chat_id=? ORDER BY s.first_seen DESC, s.{_tiebreak()} DESC"),
            (chat_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 필터
# --------------------------------------------------------------------------- #
def add_filter(chat_id: int, ftype: str, value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    with _lock:
        cur = _c().execute(_q(
            "INSERT INTO filters(chat_id, ftype, value) VALUES(?,?,?) "
            "ON CONFLICT DO NOTHING"),
            (chat_id, ftype, value))
        _c().commit()
        return cur.rowcount > 0


def remove_filter(chat_id: int, ftype: str, value: str) -> bool:
    with _lock:
        cur = _c().execute(_q("DELETE FROM filters WHERE chat_id=? AND ftype=? AND value=?"),
                           (chat_id, ftype, value.strip()))
        _c().commit()
        return cur.rowcount > 0


def clear_filters(chat_id: int, ftype: Optional[str] = None) -> None:
    with _lock:
        if ftype:
            _c().execute(_q("DELETE FROM filters WHERE chat_id=? AND ftype=?"), (chat_id, ftype))
        else:
            _c().execute(_q("DELETE FROM filters WHERE chat_id=?"), (chat_id,))
        _c().commit()


def get_filters(chat_id: int) -> Tuple[List[str], List[str]]:
    with _lock:
        rows = _c().execute(_q("SELECT ftype, value FROM filters WHERE chat_id=?"),
                            (chat_id,)).fetchall()
    kws = [r["value"] for r in rows if r["ftype"] == "keyword"]
    regs = [r["value"] for r in rows if r["ftype"] == "region"]
    return kws, regs


def list_values(chat_id: int, ftype: str) -> List[str]:
    with _lock:
        rows = _c().execute(_q("SELECT value FROM filters WHERE chat_id=? AND ftype=?"),
                            (chat_id, ftype)).fetchall()
    return [r["value"] for r in rows]


def set_scalar(chat_id: int, ftype: str, value) -> None:
    with _lock:
        _c().execute(_q("DELETE FROM filters WHERE chat_id=? AND ftype=?"), (chat_id, ftype))
        if value not in (None, ""):
            _c().execute(_q("INSERT INTO filters(chat_id, ftype, value) VALUES(?,?,?)"),
                         (chat_id, ftype, str(value)))
        _c().commit()


def get_scalar(chat_id: int, ftype: str):
    with _lock:
        row = _c().execute(_q("SELECT value FROM filters WHERE chat_id=? AND ftype=?"),
                           (chat_id, ftype)).fetchone()
    return row["value"] if row else None


def get_all_filters(chat_id: int) -> dict:
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    def _i(v):
        f = _f(v)
        return int(f) if f is not None else None
    md = get_scalar(chat_id, "max_dday")
    return {
        "sites": list_values(chat_id, "site"),
        "keywords": list_values(chat_id, "keyword"),
        "regions": list_values(chat_id, "region"),
        "categories": list_values(chat_id, "category"),
        "channels": list_values(chat_id, "channel"),
        "min_competition": _f(get_scalar(chat_id, "min_competition")),
        "max_competition": _f(get_scalar(chat_id, "max_competition")),
        "min_dday": _i(get_scalar(chat_id, "min_dday")),
        "max_dday": int(_f(md)) if md not in (None, "") else None,
        "min_recruit": _i(get_scalar(chat_id, "min_recruit")),
        "max_recruit": _i(get_scalar(chat_id, "max_recruit")),
        "min_applicants": _i(get_scalar(chat_id, "min_applicants")),
        "max_applicants": _i(get_scalar(chat_id, "max_applicants")),
    }


# --------------------------------------------------------------------------- #
# 수집 캠페인
# --------------------------------------------------------------------------- #
def seen_count(site: str) -> int:
    with _lock:
        row = _c().execute(_q("SELECT COUNT(*) AS n FROM seen WHERE site=?"),
                           (site,)).fetchone()
    return row["n"]


def meta_get(key: str) -> Optional[str]:
    with _lock:
        row = _c().execute(_q("SELECT value FROM meta WHERE key=?"), (key,)).fetchone()
    return row["value"] if row else None


def meta_set(key: str, value: str) -> None:
    with _lock:
        _c().execute(_q(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"), (key, value))
        _c().commit()


def log_visit(ip: str, path: str, referrer: str = "", ua: str = "", uid=None) -> None:
    """웹 방문 1건 기록(베스트에포트 - 실패해도 요청엔 영향 없음)."""
    try:
        with _lock:
            _c().execute(_q("INSERT INTO visits(ts,ip,path,referrer,ua,uid) VALUES(?,?,?,?,?,?)"),
                         (_now(), (ip or "")[:64], (path or "")[:200],
                          (referrer or "")[:300], (ua or "")[:400], uid))
            _c().commit()
    except Exception:
        pass


def visit_stats(days: int = 14) -> dict:
    """최근 days 일 방문 통계: 일별/전체/오늘/유입경로/페이지."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    today = datetime.date.today().isoformat()
    with _lock:
        c = _c()
        daily = c.execute(_q(
            "SELECT substr(ts,1,10) AS d, count(*) AS v, count(DISTINCT ip) AS u "
            "FROM visits WHERE ts>=? GROUP BY substr(ts,1,10) ORDER BY d DESC"), (cutoff,)).fetchall()
        tot = c.execute(_q("SELECT count(*) AS v, count(DISTINCT ip) AS u FROM visits WHERE ts>=?"),
                        (cutoff,)).fetchone()
        tod = c.execute(_q("SELECT count(*) AS v, count(DISTINCT ip) AS u FROM visits WHERE ts>=?"),
                        (today,)).fetchone()
        refs = c.execute(_q(
            "SELECT CASE WHEN referrer IS NULL OR referrer='' THEN '(직접/앱)' ELSE referrer END AS r, "
            "count(*) AS n FROM visits WHERE ts>=? GROUP BY r ORDER BY n DESC LIMIT 12"), (cutoff,)).fetchall()
        paths = c.execute(_q(
            "SELECT path, count(*) AS n FROM visits WHERE ts>=? GROUP BY path ORDER BY n DESC LIMIT 12"),
            (cutoff,)).fetchall()
    return {"days": days, "daily": [dict(r) for r in daily],
            "total": dict(tot), "today": dict(tod),
            "referrers": [dict(r) for r in refs], "paths": [dict(r) for r in paths]}


def prune_visits(days: int = 90) -> int:
    """days 일 지난 방문 로그 삭제(개인정보 보관기간 제한). 삭제 건수 반환."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    with _lock:
        cur = _c().execute(_q("DELETE FROM visits WHERE ts < ?"), (cutoff,))
        _c().commit()
        return cur.rowcount or 0


def set_adapter_health(key: str, cnt: int, status: str, note: str = "") -> None:
    """수집 결과 기록. status: ok / zero / partial / error."""
    with _lock:
        _c().execute(_q(
            "INSERT INTO adapter_health(key,ts,cnt,status,note) VALUES(?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts,cnt=excluded.cnt,"
            "status=excluded.status,note=excluded.note"),
            (key, _now(), int(cnt or 0), status, (note or "")[:200]))
        _c().commit()


def get_adapter_health() -> list:
    with _lock:
        rows = _c().execute("SELECT key,ts,cnt,status,note FROM adapter_health ORDER BY key").fetchall()
    return [dict(r) for r in rows]


def add_inquiry(uid, contact: str, message: str) -> None:
    with _lock:
        _c().execute(_q("INSERT INTO inquiries(ts,uid,contact,message,handled) VALUES(?,?,?,?,0)"),
                     (_now(), uid, (contact or "")[:120], (message or "")[:2000]))
        _c().commit()


def list_inquiries(limit: int = 100) -> list:
    with _lock:
        rows = _c().execute(_q(
            "SELECT id,ts,uid,contact,message,handled FROM inquiries "
            "ORDER BY id DESC LIMIT ?"), (limit,)).fetchall()
    return [dict(r) for r in rows]


def is_reminded(chat_id, site: str, cid: str) -> bool:
    with _lock:
        return _c().execute(_q(
            "SELECT 1 AS x FROM reminders WHERE chat_id=? AND site=? AND cid=?"),
            (chat_id, site, cid)).fetchone() is not None


def mark_reminded(chat_id, site: str, cid: str) -> None:
    with _lock:
        _c().execute(_q(
            "INSERT INTO reminders(chat_id,site,cid) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,site,cid) DO NOTHING"), (chat_id, site, cid))
        _c().commit()


def count_new_inquiries() -> int:
    with _lock:
        row = _c().execute("SELECT count(*) AS n FROM inquiries WHERE handled=0").fetchone()
    return row["n"] if row else 0


def backfill_done(site: str) -> bool:
    """해당 사이트의 '첫 전체 수집(백필)'이 끝까지 정상 완료됐는지 여부.
    완료 전(또는 중간에 끊김)에는 매 수집을 전체 크롤로 돌려 구멍을 메운다."""
    return meta_get(f"backfill_done:{site}") == "1"


def set_backfill_done(site: str) -> None:
    meta_set(f"backfill_done:{site}", "1")


def is_seen(site: str, cid: str) -> bool:
    with _lock:
        return _c().execute(_q("SELECT 1 AS x FROM seen WHERE site=? AND cid=?"),
                            (site, cid)).fetchone() is not None


def record_campaign(c) -> None:
    """캠페인 저장/갱신. first_seen 은 최초 1회만 기록(NEW 판정 기준).
    D-day·신청·모집·경쟁률 등 변동값은 매 수집마다 갱신한다.
    단, 새 값이 비어(NULL)있으면 기존 값을 보존(COALESCE) → 일시적 누락으로 덮어쓰지 않음.
    D-day 는 절대 마감일(deadline=오늘+dday)로 환산해 저장한다.
    → 화면에서는 deadline-오늘 로 계산하므로 재수집 없이도 매일 자동 감소."""
    deadline = None
    if c.dday is not None:
        deadline = (datetime.date.today() + datetime.timedelta(days=c.dday)).isoformat()
    # 지역: 제목의 '위치 대괄호'에서만 추출(유형태그·가게이름 오인 방지) → 표준화 → 카카오 캐시.
    raw_region = region_from_title(c.title or "")
    region = normalize_offline(raw_region) if raw_region else ""
    if not region and raw_region:
        region = region_cache_get(raw_region) or ""
    # 제목에서 지역을 못 찾으면 어댑터가 직접 준 region(예: 레뷰 API 의 venue 주소·local 태그)을 사용.
    if not region and getattr(c, "region", ""):
        region = normalize_offline(c.region) or region_cache_get(c.region) or ""
    # 카테고리 결정 순서: ① 내용(제목·해시태그 등) 키워드 분류 → ② 안 되면 어댑터가 준 소스 카테고리/유형
    # (배송·기자단 등) 폴백 → ③ 그것도 없으면 '기타'. 유형이 내용을 덮어쓰지 않게 내용을 우선한다
    # (예: '피부과'는 사이트 유형이 배송형이어도 뷰티). 억지로 맛집 등으로 추측하지 않음.
    # 소스가 준 표준 카테고리('기타' 제외)는 신뢰(어댑터가 소스 분류를 매핑해 넘김).
    # 없으면 제목·해시태그로 분류, 그래도 모르면 '기타'(억지 추측 없음).
    # (엉성한 유형만 주는 사이트는 어댑터가 classify_with_fallback 로 내용 우선 처리해 넘긴다)
    from .matcher import classify, CANONICAL
    if (c.category or "") in CANONICAL and c.category != "기타":
        category = c.category
    else:
        category = classify(" ".join([c.title or "", c.channel or "", getattr(c, "extra", "") or ""]))
    with _lock:
        _c().execute(_q(
            "INSERT INTO seen(site, cid, title, url, region, region_raw, category, channel, "
            "dday, applicants, recruit, competition, image, deadline, first_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(site, cid) DO UPDATE SET "
            "title=excluded.title, url=excluded.url, region=excluded.region, "
            "region_raw=excluded.region_raw, "
            "category=excluded.category, channel=excluded.channel, "
            "dday=COALESCE(excluded.dday, seen.dday), "
            "applicants=COALESCE(excluded.applicants, seen.applicants), "
            "recruit=COALESCE(excluded.recruit, seen.recruit), "
            "competition=COALESCE(excluded.competition, seen.competition), "
            "image=COALESCE(NULLIF(excluded.image, ''), seen.image), "
            "deadline=COALESCE(excluded.deadline, seen.deadline)"),
            (c.site, c.cid, c.title, c.url, region, raw_region, category, c.channel,
             c.dday, c.applicants, c.recruit, c.competition, getattr(c, "image", ""),
             deadline, _now()),
        )
        _c().commit()


# 피드 노출 조건: 마감 지난 건 숨김(마감일 미상은 유지). deadline 은 'YYYY-MM-DD' 문자열이라
# 같은 형식의 오늘 날짜와 사전식 비교 = 날짜 비교(PG/SQLite 공통).
_ACTIVE = "(deadline IS NULL OR deadline >= ?)"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def list_recent(limit: int = 200) -> List[dict]:
    """최근 수집 캠페인(최신순, 활성만=마감 안 지난 것). 피드/필터 표시에 사용."""
    with _lock:
        rows = _c().execute(_q(
            f"SELECT * FROM seen WHERE {_ACTIVE} "
            f"ORDER BY first_seen DESC, {_tiebreak()} DESC LIMIT ?"),
            (_today(), limit)).fetchall()
    return [dict(r) for r in rows]


def _order_by(sort: str) -> str:
    """정렬 ORDER BY 절. (deadline IS NULL) 은 PG/SQLite 모두 비널 먼저(NULLS LAST 효과)."""
    tb = _tiebreak()
    if sort == "deadline":      # 마감 임박순: D-day 적게 남은 것 먼저
        return f"(deadline IS NULL), deadline ASC, {tb} DESC"
    if sort == "competition":   # 경쟁률 낮은순
        return f"(competition IS NULL), competition ASC, {tb} DESC"
    # recent(기본=최신순): D-day 많이 남은 것 먼저(마감 늦은 순)
    return f"(deadline IS NULL), deadline DESC, {tb} DESC"


def list_page(offset: int, limit: int, sort: str = "recent") -> List[dict]:
    """필터 없을 때 DB 레벨 페이지네이션(활성만) + 정렬."""
    with _lock:
        rows = _c().execute(_q(
            f"SELECT * FROM seen WHERE {_ACTIVE} "
            f"ORDER BY {_order_by(sort)} LIMIT ? OFFSET ?"),
            (_today(), limit, offset)).fetchall()
    return [dict(r) for r in rows]


def count_seen() -> int:
    """피드에 보이는(활성) 캠페인 수."""
    with _lock:
        return _c().execute(_q(f"SELECT COUNT(*) AS n FROM seen WHERE {_ACTIVE}"),
                            (_today(),)).fetchone()["n"]


def list_active(sites: Optional[List[str]] = None) -> List[dict]:
    """활성(마감 안 지난) 캠페인 전체를 최신순으로. 사이트가 주어지면 DB 레벨에서 필터.
    필터 적용 피드용 — list_recent 의 최신 N건 상한이 없어 오래된 사이트도 누락되지 않는다."""
    today = _today()
    sql = f"SELECT * FROM seen WHERE {_ACTIVE}"
    params: list = [today]
    if sites:
        placeholders = ",".join(["?"] * len(sites))
        sql += f" AND site IN ({placeholders})"
        params.extend(sites)
    sql += f" ORDER BY first_seen DESC, {_tiebreak()} DESC"
    with _lock:
        rows = _c().execute(_q(sql), tuple(params)).fetchall()
    return [dict(r) for r in rows]


def purge_expired(grace_days: int = 3) -> int:
    """마감 후 grace_days 가 지난 캠페인을 DB 에서 삭제. 반환: 삭제 건수.
    (마감일 미상은 보존. grace_days 동안은 피드에선 숨겨지지만 DB 엔 남아 유예.)"""
    cutoff = (datetime.date.today() - datetime.timedelta(days=grace_days)).isoformat()
    with _lock:
        cur = _c().execute(_q(
            "DELETE FROM seen WHERE deadline IS NOT NULL AND deadline < ?"), (cutoff,))
        _c().commit()
        return cur.rowcount


def active_cids(site: str) -> set:
    """해당 사이트의 '활성(마감 전·미상)' 캠페인 cid 집합."""
    with _lock:
        rows = _c().execute(_q(
            "SELECT cid FROM seen WHERE site=? AND (deadline IS NULL OR deadline>=?)"),
            (site, _today())).fetchall()
    return {r["cid"] for r in rows}


def delete_campaigns(site: str, cids) -> int:
    """해당 사이트의 지정 cid 들을 삭제. 반환: 삭제 건수."""
    cids = list(cids)
    if not cids:
        return 0
    with _lock:
        for cid in cids:
            _c().execute(_q("DELETE FROM seen WHERE site=? AND cid=?"), (site, cid))
        _c().commit()
    return len(cids)


def count_seen_new(cutoff: str) -> int:
    """first_seen 이 cutoff(타임스탬프) 이후인(=최근 수집=NEW) 캠페인 수."""
    with _lock:
        return _c().execute(_q(
            "SELECT COUNT(*) AS n FROM seen WHERE first_seen >= ?"),
            (cutoff,)).fetchone()["n"]


def region_tree() -> dict:
    """저장된 지역을 시/도 → [구...] 로 묶어 반환(실데이터 기반).
    예: '인천 남동' → {'인천': ['남동', ...]}. 필터 UI 세분화에 사용."""
    with _lock:
        rows = _c().execute(
            "SELECT DISTINCT region FROM seen WHERE region IS NOT NULL AND region<>''"
        ).fetchall()
    tree: dict = {}
    for r in rows:
        reg = (r["region"] or "").strip()
        if not reg:
            continue
        parts = reg.split(None, 1)      # '인천 남동구' -> ['인천', '남동구']
        sido = parts[0]
        gu = parts[1].strip() if len(parts) > 1 else ""
        tree.setdefault(sido, set())
        if gu:
            tree[sido].add(gu)
    return {k: sorted(v) for k, v in tree.items()}


# --------------------------------------------------------------------------- #
# 지역 정규화 캐시(카카오 지오코딩 결과 보관) + 백필
# --------------------------------------------------------------------------- #
def region_cache_get(raw: str):
    with _lock:
        row = _c().execute(_q("SELECT norm FROM region_cache WHERE raw=?"), (raw,)).fetchone()
    return row["norm"] if row else None


def region_cache_set(raw: str, norm: str) -> None:
    with _lock:
        _c().execute(_q(
            "INSERT INTO region_cache(raw, norm) VALUES(?,?) "
            "ON CONFLICT(raw) DO UPDATE SET norm=excluded.norm"), (raw, norm))
        _c().commit()


def regions_pending(limit: int = 50) -> List[str]:
    """아직 정규화 안 됐고 카카오도 아직 시도 안 한 원본 지역들.
    이미 region_cache 에 있으면(성공/무매칭 무관) 다시 조회하지 않는다."""
    with _lock:
        rows = _c().execute(_q(
            "SELECT DISTINCT region_raw AS r FROM seen "
            "WHERE (region IS NULL OR region='') AND region_raw IS NOT NULL AND region_raw<>'' "
            "AND region_raw NOT IN (SELECT raw FROM region_cache) "
            "LIMIT ?"), (limit,)).fetchall()
    return [r["r"] for r in rows]


def set_region_for_raw(raw: str, norm: str) -> int:
    """특정 원본 지역을 가진 모든 캠페인의 region 을 갱신."""
    with _lock:
        cur = _c().execute(_q("UPDATE seen SET region=? WHERE region_raw=?"), (norm, raw))
        _c().commit()
        return cur.rowcount
