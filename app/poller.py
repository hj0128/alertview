"""주기적 수집 → (페이지 단위 즉시 저장) 신규 감지 → 알림."""
from __future__ import annotations
import asyncio
import logging
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
            deep = not db.backfill_done(ad.key)   # 백필 미완료면 전체 크롤(알림 억제)
            stats = {"fresh": 0}

            def on_page(items, ad=ad, deep=deep, stats=stats) -> bool:
                db_new = 0
                for c in items:
                    seen = db.is_seen(c.site, c.cid)
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
                continue
            if deep and not getattr(ad, "partial", False):
                db.set_backfill_done(ad.key)       # 전체 크롤 정상 완료 → 이후 증분 모드
            log.info("[%s] 신규 %d건%s", ad.key, stats["fresh"],
                     " (전체 백필: 알림생략)" if deep else "")
    return new_items


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
    await _check_mrblog_cookie(bot, demo)
    await _kakao_backfill()
    if new_items:
        sent = await notify_new(bot, new_items)
        log.info("신규 %d건 → 메시지 %d건 발송", len(new_items), sent)
