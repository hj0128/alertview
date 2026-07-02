"""주기적 수집 → (페이지 단위 즉시 저장) 신규 감지 → 알림."""
from __future__ import annotations
import asyncio
import logging
import time
from typing import List

import httpx

from . import db, config
from .adapters import active_adapters
from .adapters.base import Campaign
from .notifier import notify_new
from .region_norm import normalize_offline

log = logging.getLogger(__name__)


async def collect_new(demo: bool) -> List[Campaign]:
    """페이지가 들어오는 즉시 DB 저장(점진적 표시).
    전체 백필(deep): 전부 저장하되 알림 생략, 끝까지 크롤. 백필이 '정상 완료'된 뒤부터
    증분 모드(새 캠페인 없는 페이지에서 조기 종료, 신규는 알림). 백필이 중간에 끊기면
    (앱 재시작/요청 오류 등) 완료 플래그가 안 찍혀 다음 수집에서 다시 전체 크롤 → 구멍 자가 치유."""
    new_items: List[Campaign] = []
    async with httpx.AsyncClient(follow_redirects=True, trust_env=False) as client:
        for ad in active_adapters(demo):
            # 별도 수집주기가 설정된 어댑터(min_interval)는 주기가 안 됐으면 건너뜀(기존 데이터 유지).
            iv = getattr(ad, "min_interval", 0)
            if iv > 0:
                last = db.meta_get(f"last_poll:{ad.key}")
                if last:
                    try:
                        if time.time() - float(last) < iv:
                            continue
                    except ValueError:
                        pass
            deep = not db.backfill_done(ad.key)   # 백필 미완료면 전체 크롤(알림 억제)
            stats = {"fresh": 0}
            seen_cids = set()                      # 이번 수집에서 본 cid(자동 정리용)

            def on_page(items, ad=ad, deep=deep, stats=stats, seen_cids=seen_cids) -> bool:
                db_new = 0
                for c in items:
                    seen = db.is_seen(c.site, c.cid)
                    seen_cids.add(c.cid)
                    db.record_campaign(c)          # 항상 저장/갱신(변동값 최신화)
                    if seen:
                        continue                   # 이미 본 건: 갱신만 하고 신규 카운트 제외
                    stats["fresh"] += 1
                    db_new += 1
                    if not deep:
                        new_items.append(c)
                return True if deep else (db_new > 0)

            try:
                await ad.fetch(client, on_page=on_page)
            except Exception as e:
                # 백필이 끝까지 못 감 → 완료 플래그 미설정 → 다음 수집에서 재시도(자가 치유)
                log.warning("[%s] fetch 실패%s: %s",
                            ad.key, " (백필 미완료 → 다음 수집에 재시도)" if deep else "", e)
                db.set_adapter_health(ad.key, 0, "error", str(e))
                continue
            if deep and not getattr(ad, "partial", False):
                db.set_backfill_done(ad.key)       # 전체 크롤 정상 완료 → 이후 증분 모드
            if iv > 0:
                db.meta_set(f"last_poll:{ad.key}", str(time.time()))   # 주기 어댑터: 마지막 수집시각 기록
            # 수집 건강 상태 기록: 0건=zero(소스 이상 의심), 부분수집=partial, 정상=ok
            cnt = len(seen_cids)
            if getattr(ad, "partial", False) or getattr(ad, "cookie_expired", False):
                status = "partial"
            elif cnt == 0:
                status = "zero"
            else:
                status = "ok"
            db.set_adapter_health(ad.key, cnt, status)
            log.info("[%s] 신규 %d건%s", ad.key, stats["fresh"],
                     " (전체 백필: 알림생략)" if deep else "")
            _prune_unseen(ad, seen_cids)           # 소스에서 내려간 활성 캠페인 자동 삭제
    return new_items


_PRUNE_MIN_RATIO = 0.5     # 이번 수집량이 기존 활성의 이 비율 미만이면 정리 보류(부분수집 오삭제 방지)


def _prune_unseen(ad, seen_cids: set) -> None:
    """전체 목록을 완주하는(prunable) 어댑터에 한해, 이번 수집에서 안 보인 활성 캠페인을 삭제.
    = 소스에서 내려간(마감/삭제된) 캠페인 정리. 부분수집(네트워크 장애 등) 시 대량 오삭제를
    막기 위해, 수집량이 기존 활성의 절반 미만이거나 어댑터가 부분/만료 상태면 건너뛴다."""
    if not getattr(ad, "prunable", False):
        return
    if getattr(ad, "partial", False) or getattr(ad, "cookie_expired", False):
        log.info("[%s] 부분 수집 상태 → 자동 정리 보류", ad.key)
        return
    active = db.active_cids(ad.key)
    if len(seen_cids) < max(10, int(len(active) * _PRUNE_MIN_RATIO)):
        log.warning("[%s] 수집량(%d)이 기존 활성(%d)의 절반 미만 → 자동 정리 보류(부분수집 의심)",
                    ad.key, len(seen_cids), len(active))
        return
    stale = active - seen_cids
    if stale:
        deleted = db.delete_campaigns(ad.key, stale)
        log.info("[%s] 소스에서 내려간 캠페인 %d건 자동 삭제", ad.key, deleted)


_mrblog_alerted = False    # 쿠키 만료 알림 중복 방지(만료 상태 동안 1회만)


async def _check_mrblog_cookie(bot, demo: bool) -> None:
    """미블 세션 쿠키 만료 시 관리자(ADMIN_CHAT_ID)에게만 1회 알림."""
    global _mrblog_alerted
    if not (bot and config.ADMIN_CHAT_ID):
        return
    ad = next((a for a in active_adapters(demo) if getattr(a, "key", "") == "mrblog"), None)
    if ad is None:
        return
    if getattr(ad, "cookie_expired", False):
        if not _mrblog_alerted:
            try:
                await bot.send_message(
                    chat_id=int(config.ADMIN_CHAT_ID),
                    text=("⚠️ 미블 세션 쿠키가 만료된 것 같아요.\n"
                          ".env 의 MRBLOG_COOKIE 를 새로 로그인한 값으로 교체하고 재시작해 주세요.\n"
                          "(지금은 미블 홈 공개분만 수집 중이며, 다른 사이트는 정상입니다.)"))
                _mrblog_alerted = True
                log.info("미블 쿠키 만료 알림 발송(관리자)")
            except Exception as e:
                log.warning("미블 만료 알림 실패: %s", e)
    else:
        _mrblog_alerted = False    # 정상 복구 시 다음 만료 때 다시 알릴 수 있도록 리셋


_health_alerted: set = set()


async def _check_adapter_health(bot) -> None:
    """수집 이상(에러/0건) 어댑터를 관리자에게 알림. 상태 전환 시 1회(복구되면 자동 해제)."""
    global _health_alerted
    if not (bot and config.ADMIN_CHAT_ID):
        return
    bad = {h["key"]: h for h in db.get_adapter_health() if h["status"] in ("error", "zero")}
    for key in set(bad) - _health_alerted:
        h = bad[key]
        try:
            await bot.send_message(
                chat_id=int(config.ADMIN_CHAT_ID),
                text=(f"⚠️ 수집 이상: [{key}] → {h['status']}"
                      + (f"\n{h['note']}" if h.get("note") else "")
                      + "\n(/admin 에서 사이트별 상태 확인)"))
            log.info("헬스 이상 알림 발송: %s (%s)", key, h["status"])
        except Exception as e:
            log.warning("헬스 알림 실패(%s): %s", key, e)
    _health_alerted = set(bad)


async def _kakao_backfill() -> None:
    """내장 사전이 못 잡은 지역(역/랜드마크 등)을 카카오 로컬 API로 보정 후 캐시."""
    key = config.KAKAO_REST_API_KEY
    if not key:
        return
    pend = db.regions_pending(50)
    if not pend:
        return
    headers = {"Authorization": f"KakaoAK {key}"}
    filled = 0
    async with httpx.AsyncClient(trust_env=False, timeout=15.0) as client:
        for raw in pend:
            norm = db.region_cache_get(raw)
            if norm is None:                 # 아직 조회 안 한 원본
                norm = ""
                try:
                    r = await client.get(
                        "https://dapi.kakao.com/v2/local/search/keyword.json",
                        params={"query": raw, "size": 1}, headers=headers)
                    if r.status_code == 200:
                        docs = r.json().get("documents") or []
                        if docs:
                            addr = docs[0].get("address_name") or docs[0].get("road_address_name") or ""
                            norm = normalize_offline(addr)
                    elif r.status_code in (401, 403):
                        # 키/권한/IP 문제 → 항목마다 재시도 무의미. 이번 주기 즉시 중단.
                        log.warning("[kakao] 인증 거부(HTTP %s). 카카오 콘솔에서 '카카오맵' 사용 설정 ON, "
                                    "보안 '허용 IP' 비우기(또는 서버 IP 등록), REST API 키 확인 필요. "
                                    "→ 이번 주기 중단", r.status_code)
                        return
                    else:
                        log.warning("[kakao] HTTP %s ('%s')", r.status_code, raw)
                        continue            # 일시 오류는 캐시하지 않음(다음에 재시도)
                except httpx.ConnectError as e:
                    # DNS 해석/연결 실패(컨테이너 외부망 일시 단절). 남은 항목도 다 실패하므로
                    # 이번 주기 즉시 중단(실패는 캐시 안 함 → 다음 주기 자동 재시도).
                    log.warning("[kakao] 네트워크/DNS 오류(%s) → 이번 주기 중단", e)
                    return
                except Exception as e:
                    log.warning("[kakao] '%s' 지오코딩 실패: %s", raw, e)
                    continue
                # 카카오도 못 잡으면 '기타'로 분류(위치 표기는 있었으나 매핑 실패).
                norm = norm or "기타"
                db.region_cache_set(raw, norm)   # 결과 캐시 → 반복 호출 방지
            else:
                norm = norm or "기타"            # 과거 빈 캐시도 '기타'로
            db.set_region_for_raw(raw, norm)
            filled += 1
            await asyncio.sleep(0.1)
    if filled:
        log.info("[kakao] 지역 보정 %d건 적용", filled)


async def run_poll(bot, demo: bool) -> None:
    new_items = await collect_new(demo)
    if config.PURGE_GRACE_DAYS > 0:
        purged = db.purge_expired(config.PURGE_GRACE_DAYS)
        if purged:
            log.info("마감 %d일 지난 캠페인 %d건 삭제", config.PURGE_GRACE_DAYS, purged)
    dropped = db.prune_visits(90)          # 방문 로그 90일 보관 후 자동삭제(개인정보)
    if dropped:
        log.info("방문 로그 90일 지난 %d건 삭제", dropped)
    await _check_mrblog_cookie(bot, demo)
    await _check_adapter_health(bot)
    await _kakao_backfill()
    if new_items:
        sent = await notify_new(bot, new_items)
        log.info("신규 %d건 → 메시지 %d건 발송", len(new_items), sent)
