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

import logging
import sqlite3
import threading
import time
from typing import List, Optional, Tuple

from . import config

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
    """캠페인 상세를 저장(신규일 때만 first_seen 기록)."""
    with _lock:
        _c().execute(_q(
            "INSERT INTO seen(site, cid, title, url, region, category, channel, "
            "dday, applicants, recruit, competition, image, first_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING"),
            (c.site, c.cid, c.title, c.url, c.region, c.category, c.channel,
             c.dday, c.applicants, c.recruit, c.competition, getattr(c, "image", ""), _now()),
        )
        _c().commit()


def list_recent(limit: int = 200) -> List[dict]:
    """최근 수집 캠페인(최신순). 피드/필터 표시에 사용."""
    with _lock:
        rows = _c().execute(_q(
            f"SELECT * FROM seen ORDER BY first_seen DESC, {_tiebreak()} DESC LIMIT ?"),
            (limit,)).fetchall()
    return [dict(r) for r in rows]
