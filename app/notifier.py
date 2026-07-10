"""텔레그램 메시지 포맷 + 발송."""
from __future__ import annotations
import asyncio
import datetime
import logging
from typing import List

from telegram import Bot
from telegram.constants import ParseMode

from .adapters.base import Campaign
from . import db, config
from .matcher import matches_filter

log = logging.getLogger(__name__)


def format_campaign(c: Campaign) -> str:
    bits = []
    if c.region:
        bits.append(f"📍 {c.region}")
    if c.category:
        bits.append(f"🏷 {c.category}")
    if c.channel:
        bits.append(f"📝 {c.channel}")
    line2 = "   ".join(bits)
    stat = []
    if c.dday is not None:
        stat.append(f"⏰ D-{c.dday}")
    if c.competition is not None:
        stat.append(f"🔥 경쟁률 {c.competition}")
    if c.applicants is not None and c.recruit is not None:
        stat.append(f"👥 {c.applicants}/{c.recruit}")
    line3 = "   ".join(stat)
    out = [f"🆕 <b>새 체험단</b> · {c.site_name}", "", c.title]
    if line2:
        out.append(line2)
    if line3:
        out.append(line3)
    out.append(f"🔗 {c.url}")
    return "\n".join(out)


def _format_reminder(r: dict, dleft: int) -> str:
    dtxt = "오늘 마감" if dleft <= 0 else f"D-{dleft} (내일 마감)"
    bits = []
    if r.get("region"):
        bits.append(f"📍 {r['region']}")
    if r.get("competition") is not None:
        bits.append(f"🔥 경쟁률 {r['competition']}")
    out = [f"⏰ <b>찜한 체험단 마감임박</b> · {dtxt}", "", (r.get("title") or "").strip()]
    if bits:
        out.append("   ".join(bits))
    out.append(f"🔗 {r.get('url') or ''}")
    return "\n".join(out)


async def remind_deadlines(bot: Bot) -> int:
    """활성 사용자의 '찜한' 캠페인 중 마감 임박(D-1~D-0)인 것을 1회 리마인드."""
    if not bot:
        return 0
    today = datetime.date.today()
    sent = 0
    for chat_id in db.active_users():
        quiet = _in_quiet_hours(chat_id)        # 사용자별 방해금지
        for r in db.favorites_rows(chat_id):
            dl = r.get("deadline")
            if not dl:
                continue
            try:
                dleft = (datetime.date.fromisoformat(dl) - today).days
            except (ValueError, TypeError):
                continue
            if not (0 <= dleft <= 1):
                continue
            if db.is_reminded(chat_id, r["site"], r["cid"]):
                continue
            text = _format_reminder(r, dleft)
            if quiet:                                    # 밤엔 대기열(개별) + 처리표시(중복 방지)
                db.enqueue_notify(chat_id, text, kind="reminder")
                db.mark_reminded(chat_id, r["site"], r["cid"])
            elif await _send(bot, chat_id, text):
                db.mark_reminded(chat_id, r["site"], r["cid"])
                sent += 1
    return sent


def _in_quiet_hours(chat_id=None) -> bool:
    """방해금지(밤) 시간대인가. 사용자가 직접 설정했으면 그 값, 아니면 전역 기본(config).
    START==END 면 비활성. 자정 넘김 지원(예: 23~8)."""
    s, e = config.QUIET_START, config.QUIET_END
    if chat_id is not None:
        us, ue = db.get_quiet(chat_id)
        if us is not None and ue is not None:
            s, e = us, ue
    if s == e:
        return False
    h = datetime.datetime.now().hour
    return (s <= h < e) if s < e else (h >= s or h < e)


async def _send(bot: Bot, chat_id, text: str) -> bool:
    try:
        await bot.send_message(chat_id=chat_id, text=text,
                               parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        await asyncio.sleep(config.SEND_GAP)
        return True
    except Exception as e:
        log.warning("send fail chat_id=%s: %s", chat_id, e)
        return False


async def flush_pending(bot: Bot) -> int:
    """방해금지 종료 후 대기열 발송: 신규는 사용자당 '요약 1건', 리마인더는 개별.
    알림 끈 사용자(active=0)에겐 보내지 않고 큐에서 제거."""
    if not bot:
        return 0
    active = set(db.active_users())
    news: dict = {}      # chat_id -> [(id, line)]  (신규 요약용)
    sent = 0
    for row in db.list_pending():
        cid = row["chat_id"]
        if cid not in active:
            db.delete_pending(row["id"])            # 알림 끈 사용자 → 발송 안 함
            continue
        if _in_quiet_hours(cid):
            continue                                # 아직 이 사용자 방해금지 → 대기 유지
        if row.get("kind") == "reminder":
            await _send(bot, cid, row["text"])      # 리마인더는 개별(중요·소수)
            db.delete_pending(row["id"])
            sent += 1
        else:
            news.setdefault(cid, []).append((row["id"], row["text"]))
    for cid, items in news.items():                 # 밤새 신규 → 사용자당 요약 1건
        n = len(items)
        digest = f"🌙 <b>밤새 새 체험단 {n}건</b>\n\n" + "\n".join(t for _, t in items[:5])
        if n > 5:
            digest += f"\n\n… 외 {n - 5}건. 전체는 피드에서 확인하세요."
        if await _send(bot, cid, digest):
            sent += 1
        for pid, _ in items:
            db.delete_pending(pid)
    return sent


def _live_dday(r: dict):
    dl = r.get("deadline")
    if dl:
        try:
            d = (datetime.date.fromisoformat(dl) - datetime.date.today()).days
            return d if d >= 0 else None
        except (ValueError, TypeError):
            pass
    return r.get("dday")


def _format_reco(rows: list) -> str:
    out = ["🎯 <b>오늘의 추천</b> · 당첨 확률 높은 체험단 (경쟁률 낮은 순)", ""]
    for i, r in enumerate(rows, 1):
        bits = [f"🔥 경쟁률 {r['competition']}"]
        if r.get("applicants") is not None and r.get("recruit") is not None:
            bits.append(f"👥 {r['applicants']}/{r['recruit']}")
        dd = _live_dday(r)
        if dd is not None:
            bits.append(f"⏰ D-{dd}")
        out.append(f"{i}. {(r.get('title') or '').strip()}")
        out.append("   " + " · ".join(bits))
        out.append(f"   🔗 {r.get('url') or ''}")
    return "\n".join(out)


async def recommend_low_competition(bot: Bot, max_per_user: int = 5, threshold: float = 1.0) -> int:
    """매일 1회(방해금지 종료 후), 사용자 필터에 맞는 경쟁률 낮은(당첨확률 높은) 캠페인 추천."""
    if not bot:
        return 0
    today = datetime.date.today().isoformat()
    if db.meta_get("recommend_date") == today:      # 오늘 이미 발송
        return 0
    sent = 0
    for chat_id in db.active_users():
        if _in_quiet_hours(chat_id):                # 방해금지 중인 사용자는 이번엔 건너뜀
            continue
        f = db.get_all_filters(chat_id)
        cands = []
        for r in db.list_active(sites=f.get("sites") or None):
            comp = r["competition"]
            if comp is None or comp > threshold:     # 경쟁률 미상/높음 제외
                continue
            c = Campaign(
                site=r["site"], site_name="", cid=r["cid"], title=r["title"] or "",
                url=r["url"] or "", region=r["region"] or "", category=r["category"] or "",
                channel=r["channel"] or "", dday=_live_dday(r), applicants=r["applicants"],
                recruit=r["recruit"], competition=comp)
            if matches_filter(c, f):
                cands.append(dict(r))
        if not cands:
            continue
        cands.sort(key=lambda x: (x["competition"], _live_dday(x) if _live_dday(x) is not None else 999))
        if await _send(bot, chat_id, _format_reco(cands[:max_per_user])):
            sent += 1
    db.meta_set("recommend_date", today)             # 대상이 없어도 오늘은 시도 완료로 표시(하루 1회)
    return sent


async def send_daily_visit_report(bot: Bot) -> int:
    """매일 1회(방해금지 종료 후) 관리자에게 방문 리포트 발송. 방문 0명이어도 보냄."""
    if not bot or not config.ADMIN_CHAT_ID:
        return 0
    try:
        if _in_quiet_hours(int(config.ADMIN_CHAT_ID)):   # 관리자 방해금지 존중
            return 0
    except ValueError:
        pass
    today = datetime.date.today().isoformat()
    if db.meta_get("visit_report_date") == today:
        return 0
    s = db.visit_stats(7)
    yday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    yv = yu = 0
    for d in s["daily"]:
        if d["d"] == yday:
            yv, yu = d["v"], d["u"]
            break
    text = (f"📊 <b>방문 리포트</b>\n\n"
            f"어제({yday[5:]}) 방문 <b>{yv}</b>회 · 순 방문자 <b>{yu}</b>명\n"
            f"최근 7일 방문 {s['total']['v']}회 · 순 {s['total']['u']}명\n"
            f"오늘(지금까지) {s['today']['v']}회 · 순 {s['today']['u']}명\n\n"
            f"자세히 → 웹 로그인 후 /admin/stats")
    try:
        await bot.send_message(chat_id=int(config.ADMIN_CHAT_ID), text=text, parse_mode=ParseMode.HTML)
        log.info("방문 리포트 발송(관리자)")
    except Exception as e:
        log.warning("방문 리포트 발송 실패: %s", e)
        return 0
    db.meta_set("visit_report_date", today)
    return 1


async def notify_new(bot: Bot, campaigns: List[Campaign]) -> int:
    if not campaigns:
        return 0
    sent = 0
    for chat_id in db.active_users():
        quiet = _in_quiet_hours(chat_id)        # 사용자별 방해금지
        f = db.get_all_filters(chat_id)
        for c in campaigns:
            if matches_filter(c, f):
                if quiet:
                    # 밤엔 개별 발송 대신 요약용 한 줄로 저장 → 아침에 1건으로 묶어 발송
                    line = "· " + (c.title or "").strip() + (f"\n  🔗 {c.url}" if c.url else "")
                    db.enqueue_notify(chat_id, line, kind="new")
                elif await _send(bot, chat_id, format_campaign(c)):
                    sent += 1
    return sent
