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
        """
        CREATE TABLE IF NOT EXISTS region_cache (
            raw TEXT PRIMARY KEY, norm TEXT
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
    md = get_scalar(chat_id, "max_dday")
    return {
        "sites": list_values(chat_id, "site"),
        "keywords": list_values(chat_id, "keyword"),
        "regions": list_values(chat_id, "region"),
        "categories": list_values(chat_id, "category"),
        "channels": list_values(chat_id, "channel"),
        "max_competition": _f(get_scalar(chat_id, "max_competition")),
        "max_dday": int(_f(md)) if md not in (None, "") else None,
    }


# --------------------------------------------------------------------------- #
# 수집 캠페인
# --------------------------------------------------------------------------- #
def seen_count(site: str) -> int:
    with _lock:
        row = _c().execute(_q("SELECT COUNT(*) AS n FROM seen WHERE site=?"),
                           (site,)).fetchone()
    return row["n"]


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
            (c.site, c.cid, c.title, c.url, region, raw_region, c.category, c.channel,
             c.dday, c.applicants, c.recruit, c.competition, getattr(c, "image", ""),
             deadline, _now()),
        )
        _c().commit()


def list_recent(limit: int = 200) -> List[dict]:
    """최근 수집 캠페인(최신순). 피드/필터 표시에 사용."""
    with _lock:
        rows = _c().execute(_q(
            f"SELECT * FROM seen ORDER BY first_seen DESC, {_tiebreak()} DESC LIMIT ?"),
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def list_page(offset: int, limit: int) -> List[dict]:
    """최신순 페이지(필터 없을 때 DB 레벨 페이지네이션용)."""
    with _lock:
        rows = _c().execute(_q(
            f"SELECT * FROM seen ORDER BY first_seen DESC, {_tiebreak()} DESC LIMIT ? OFFSET ?"),
            (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def count_seen() -> int:
    with _lock:
        return _c().execute("SELECT COUNT(*) AS n FROM seen").fetchone()["n"]


def count_seen_new(date_prefix: str) -> int:
    """first_seen 이 오늘인(=NEW) 캠페인 수."""
    with _lock:
        return _c().execute(_q(
            "SELECT COUNT(*) AS n FROM seen WHERE first_seen LIKE ?"),
            (date_prefix + "%",)).fetchone()["n"]


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
