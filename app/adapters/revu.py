"""레뷰(revu.net) 어댑터.

revu.net 은 AngularJS SPA 라 HTML 엔 캠페인이 없고, 데이터는 api.weble.net JSON API 로 받는다.
목록 API 는 로그인 토큰(JWT, Bearer)을 요구하므로:
  1) POST https://api.weble.net/tokens  (헤더 Basic beep:boop, 본문 {username,password,remember})
     → {token(JWT, 수명 약 15일), sso_access_token, sso_refresh_token}
  2) GET  https://api.weble.net/v1/campaigns?type=play&sort=latest&page=N&limit=L  (헤더 Bearer token)
     → {items:[...], total, count, page, limit}
토큰은 인스턴스에 캐시하고, 401(만료) 시 1회 재로그인한다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import httpx

from .. import config
from .base import BaseAdapter, Campaign, UA

API = "https://api.weble.net"
WWW = "https://www.revu.net"
# 미인증 요청용 앱 공통 Basic 자격(공개 JS 의 $http 기본 헤더값 그대로).
BASIC = "Basic YmVlcDpib29w"
PER_PAGE = 100
log = logging.getLogger(__name__)

# revu media → 우리 채널 표준명. 미상은 빈값(=피드에서 '기타'로 분류).
_MEDIA = {
    "blog": "블로그", "naverblog": "블로그",
    "instagram": "인스타", "insta": "인스타",
    "youtube": "유튜브", "clip": "클립",
    "reels": "릴스", "shorts": "숏츠", "tiktok": "틱톡",
}


def _to_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _region_of(it: dict) -> str:
    """방문형은 venue 주소, 그 외엔 local 태그(시/도)를 지역으로. (record_campaign 이 표준화)"""
    v = it.get("venue") or {}
    addr = " ".join(str(v.get(k)) for k in ("addressFirst", "addressLast") if v.get(k)).strip()
    if addr:
        return addr
    local = it.get("localTag") or []
    return str(local[0]) if local else ""


def _to_campaign(it: dict) -> Optional[Campaign]:
    cid = it.get("id")
    if cid is None:
        return None
    title = (it.get("title") or it.get("item") or "").strip()
    if not title:
        return None
    recruit = _to_int(it.get("reviewerLimit"))
    applicants = _to_int((it.get("campaignStats") or {}).get("requestCount"))
    competition = round(applicants / recruit, 1) if recruit and applicants is not None else None
    cat = it.get("category") or []
    return Campaign(
        site="revu", site_name="레뷰", cid=str(cid), title=title,
        url=f"{WWW}/campaign/{cid}",
        region=_region_of(it),
        category=" ".join(map(str, cat)) if isinstance(cat, list) else str(cat or ""),
        channel=_MEDIA.get((it.get("media") or "").lower(), ""),
        dday=_to_int(it.get("byDeadline")),
        applicants=applicants, recruit=recruit, competition=competition,
        image=it.get("thumbnail") or "",
    )


class RevuAdapter(BaseAdapter):
    key = "revu"
    name = "레뷰"
    enabled = bool(config.REVU_USERNAME and config.REVU_PASSWORD)
    prunable = True          # type=play 전체를 total 까지 완주 → 소스에서 내려간 건 자동 삭제 가능

    def __init__(self) -> None:
        self._token: Optional[str] = None

    async def _login(self, client: httpx.AsyncClient) -> Optional[str]:
        try:
            r = await client.post(
                f"{API}/tokens",
                headers={"User-Agent": UA, "Origin": WWW, "Referer": WWW + "/",
                         "Authorization": BASIC, "Content-Type": "application/json"},
                json={"username": config.REVU_USERNAME,
                      "password": config.REVU_PASSWORD, "remember": True},
                timeout=20.0)
        except Exception as e:
            log.warning("[revu] 로그인 요청 실패: %s", e)
            return None
        if r.status_code not in (200, 201):
            log.warning("[revu] 로그인 실패 %s: %s", r.status_code, r.text[:120])
            return None
        self._token = (r.json() or {}).get("token")
        log.info("[revu] 로그인 성공")
        return self._token

    async def _get_page(self, client: httpx.AsyncClient, page: int) -> Optional[dict]:
        """캠페인 목록 한 페이지. 401 이면 재로그인 후 1회 재시도."""
        url = (f"{API}/v1/campaigns?class=campaign&type=play&sort=latest"
               f"&page={page}&limit={PER_PAGE}")
        for attempt in (1, 2):
            if not self._token and not await self._login(client):
                return None
            try:
                r = await client.get(url, headers={
                    "User-Agent": UA, "Origin": WWW, "Referer": WWW + "/",
                    "Authorization": f"Bearer {self._token}"}, timeout=20.0)
            except Exception as e:
                log.warning("[revu] page %d 요청 실패: %s", page, e)
                return None
            if r.status_code == 401 and attempt == 1:
                self._token = None          # 만료 → 재로그인 후 재시도
                continue
            if r.status_code != 200:
                log.warning("[revu] page %d 응답 %s", page, r.status_code)
                return None
            return r.json()
        return None

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        if not self.enabled:
            return []
        log.info("[revu] 수집 시작...")
        out: List[Campaign] = []
        first = await self._get_page(client, 1)
        if not first:
            return []
        total = _to_int(first.get("total")) or 0
        last_page = (total + PER_PAGE - 1) // PER_PAGE if total else 1
        if config.REVU_MAX_PAGES > 0:
            last_page = min(last_page, config.REVU_MAX_PAGES)

        def _emit(data: dict) -> None:
            page_cs = [c for c in map(_to_campaign, data.get("items") or []) if c]
            out.extend(page_cs)
            if on_page and page_cs:
                on_page(page_cs)

        _emit(first)
        for page in range(2, last_page + 1):
            await asyncio.sleep(0.3)
            data = await self._get_page(client, page)
            if not data or not (data.get("items")):
                break
            _emit(data)
            log.info("[revu] page %d/%d (누적 %d건)", page, last_page, len(out))
        log.info("[revu] 수집 완료 (총 %d건 / 오픈 %d건)", len(out), total)
        return out
