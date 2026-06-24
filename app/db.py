"""SQLite 저장소: 사용자 / 필터 / 수집 캠페인(seen) / 본 항목(viewed)."""
from __future__ import annotations
import sqlite3
import threading
import time
from typing import List, Optional, Tuple

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def init(db_path: str) -> None:
    global _conn
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL;")
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id    INTEGER PRIMARY KEY,
            active     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            seen_until TEXT
        );
        CREATE TABLE IF NOT EXISTS filters (
            chat_id INTEGER NOT NULL,
            ftype   TEXT NOT NULL,
            value   TEXT NOT NULL,
            UNIQUE(chat_id, ftype, value)
        );
        CREATE TABLE IF NOT EXISTS seen (
            site TEXT NOT NULL, cid TEXT NOT NULL,
            title TEXT, url TEXT,
            region TEXT, category TEXT, channel TEXT,
            dday INTEGER, applicants INTEGER, recruit INTEGER, competition REAL, image TEXT,
            first_seen TEXT,
            PRIMARY KEY(site, cid)
        );
        CREATE TABLE IF NOT EXISTS viewed (
            chat_id INTEGER NOT NULL, site TEXT NOT NULL, cid TEXT NOT NULL,
            PRIMARY KEY(chat_id, site, cid)
        );
        """
    )
    _migrate()
    _conn.commit()


def _migrate() -> None:
    """기존 DB에 누락된 컬럼을 추가(하위호환)."""
    def cols(t):
        return {r[1] for r in _conn.execute(f"PRAGMA table_info({t})")}
    add = [
        ("users", "seen_until", "seen_until TEXT"),
        ("seen", "region", "region TEXT"), ("seen", "category", "category TEXT"),
        ("seen", "channel", "channel TEXT"), ("seen", "dday", "dday INTEGER"),
        ("seen", "applicants", "applicants INTEGER"), ("seen", "recruit", "recruit INTEGER"),
        ("seen", "competition", "competition REAL"),
        ("seen", "image", "image TEXT"),
    ]
    for table, col, decl in add:
        if col not in cols(table):
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")
    # 기존 사용자: seen_until 없으면 지금 시점으로 백필(과거 캠페인 NEW 폭탄 방지)
    _conn.execute("UPDATE users SET seen_until=? WHERE seen_until IS NULL", (_now(),))


def _c() -> sqlite3.Connection:
    assert _conn is not None, "db.init() 를 먼저 호출하세요"
    return _conn


# ---- 사용자 ----
def upsert_user(chat_id: int) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO users(chat_id, active, created_at, seen_until) VALUES(?,1,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET active=1",
            (chat_id, _now(), _now()),
        )
        _c().commit()


def set_active(chat_id: int, active: bool) -> None:
    with _lock:
        _c().execute("UPDATE users SET active=? WHERE chat_id=?", (1 if active else 0, chat_id))
        _c().commit()


def active_users() -> List[int]:
    with _lock:
        rows = _c().execute("SELECT chat_id FROM users WHERE active=1").fetchall()
    return [r[0] for r in rows]


def is_active(chat_id: int) -> bool:
    with _lock:
        row = _c().execute("SELECT active FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return bool(row and row[0])


def get_seen_until(chat_id: int) -> str:
    with _lock:
        row = _c().execute("SELECT seen_until FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return (row[0] if row and row[0] else "0000-00-00 00:00:00")


def mark_all_seen(chat_id: int) -> None:
    """'모두 확인': 현재 시점까지 모두 본 것으로. 개별 viewed 기록은 정리.
    사용자 행이 없어도 확실히 반영되도록 upsert 한다."""
    with _lock:
        _c().execute(
            "INSERT INTO users(chat_id, active, created_at, seen_until) VALUES(?,1,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET seen_until=excluded.seen_until",
            (chat_id, _now(), _now()),
        )
        _c().execute("DELETE FROM viewed WHERE chat_id=?", (chat_id,))
        _c().commit()


# ---- 본 항목(개별) ----
def mark_viewed(chat_id: int, site: str, cid: str) -> None:
    with _lock:
        _c().execute(
            "INSERT OR IGNORE INTO viewed(chat_id, site, cid) VALUES(?,?,?)",
            (chat_id, site, cid),
        )
        _c().commit()


def viewed_set(chat_id: int) -> set:
    with _lock:
        rows = _c().execute(
            "SELECT site, cid FROM viewed WHERE chat_id=?", (chat_id,)
        ).fetchall()
    return {(r[0], r[1]) for r in rows}


# ---- 필터 ----
def add_filter(chat_id: int, ftype: str, value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    with _lock:
        try:
            _c().execute("INSERT INTO filters(chat_id, ftype, value) VALUES(?,?,?)",
                         (chat_id, ftype, value))
            _c().commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_filter(chat_id: int, ftype: str, value: str) -> bool:
    with _lock:
        cur = _c().execute("DELETE FROM filters WHERE chat_id=? AND ftype=? AND value=?",
                           (chat_id, ftype, value.strip()))
        _c().commit()
        return cur.rowcount > 0


def clear_filters(chat_id: int, ftype: Optional[str] = None) -> None:
    with _lock:
        if ftype:
            _c().execute("DELETE FROM filters WHERE chat_id=? AND ftype=?", (chat_id, ftype))
        else:
            _c().execute("DELETE FROM filters WHERE chat_id=?", (chat_id,))
        _c().commit()


def get_filters(chat_id: int) -> Tuple[List[str], List[str]]:
    with _lock:
        rows = _c().execute("SELECT ftype, value FROM filters WHERE chat_id=?", (chat_id,)).fetchall()
    kws = [v for t, v in rows if t == "keyword"]
    regs = [v for t, v in rows if t == "region"]
    return kws, regs


def list_values(chat_id: int, ftype: str) -> List[str]:
    with _lock:
        rows = _c().execute("SELECT value FROM filters WHERE chat_id=? AND ftype=?",
                            (chat_id, ftype)).fetchall()
    return [r[0] for r in rows]


def set_scalar(chat_id: int, ftype: str, value) -> None:
    with _lock:
        _c().execute("DELETE FROM filters WHERE chat_id=? AND ftype=?", (chat_id, ftype))
        if value not in (None, ""):
            _c().execute("INSERT INTO filters(chat_id, ftype, value) VALUES(?,?,?)",
                         (chat_id, ftype, str(value)))
        _c().commit()


def get_scalar(chat_id: int, ftype: str):
    with _lock:
        row = _c().execute("SELECT value FROM filters WHERE chat_id=? AND ftype=?",
                           (chat_id, ftype)).fetchone()
    return row[0] if row else None


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


# ---- 수집 캠페인 ----
def seen_count(site: str) -> int:
    with _lock:
        return _c().execute("SELECT COUNT(*) FROM seen WHERE site=?", (site,)).fetchone()[0]


def is_seen(site: str, cid: str) -> bool:
    with _lock:
        return _c().execute("SELECT 1 FROM seen WHERE site=? AND cid=?",
                            (site, cid)).fetchone() is not None


def record_campaign(c) -> None:
    """캠페인 상세를 저장(신규일 때만 first_seen 기록)."""
    with _lock:
        _c().execute(
            "INSERT OR IGNORE INTO seen(site, cid, title, url, region, category, channel, "
            "dday, applicants, recruit, competition, image, first_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.site, c.cid, c.title, c.url, c.region, c.category, c.channel,
             c.dday, c.applicants, c.recruit, c.competition, getattr(c, "image", ""), _now()),
        )
        _c().commit()


def list_recent(limit: int = 200) -> List[dict]:
    """최근 수집 캠페인(최신순). 피드/필터 표시에 사용."""
    with _lock:
        rows = _c().execute(
            "SELECT * FROM seen ORDER BY first_seen DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
