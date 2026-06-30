"""포포몬(popomon.com) 어댑터. Next.js SPA 지만 데이터는 JSON API 로 받는다(브라우저 불필요).

목록: GET /api_p/campaign/fetch_getcampaignlist
        ?searchAlign=latest&bigRecruitType=Lall&recruitType=all&interestsFilter=ALL&pageNum=N&snsSubFilter=
  → {"data":{"contentsData":[{C_idx,C_title,C_provision,C_choice_count,C_volunteer_count,
       C_regi_end_date,C_regi_end_date_count,C_recruit_type,CS_type,CT_type,thumb_img,C_state}, ...]}}
  pageNum 은 12 단위 오프셋(0,12,24...). 활성(모집중)만 반환하므로 빈 페이지를 만나면 종료.
  bigRecruitType=Lall&recruitType=all 이면 방문/배송/구매/기자단 전부 한 스트림으로 나온다.
캠페인이 매우 많아 별도 주기(기본 30분)로만 수집.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
import time
from typing import List, Optional

import httpx

from .base import BaseAdapter, Campaign, UA

BASE = "https://popomon.com"
LIST = (BASE + "/api_p/campaign/fetch_getcampaignlist?searchAlign=latest"
        "&bigRecruitType=Lall&recruitType=all&interestsFilter=ALL&pageNum={off}&snsSubFilter=")
PAGE = 12                 # 응답 고정 페이지 크기(서버가 count 파라미터 무시)
MAX_PAGES = 400           # 안전 상한(빈 페이지면 그 전에 종료)
log = logging.getLogger(__name__)

# CS_type(플랫폼) → 우리 채널. 부분일치(접두/포함)로 판정.
_CH_RULES = [("REELS", "릴스"), ("SHORTS", "숏츠"), ("CLIP", "클립"),
             ("YOUTUBE", "유튜브"), ("INSTAGRAM", "인스타"), ("BLOG", "블로그")]
# CT_type(콘텐츠 분류) → 표준 카테고리(방문형일 때만 사용)
_CT_CAT = {"RESTAURANT": "맛집", "CAFE": "카페", "FOOD": "맛집",
           "ROOMS": "숙박", "LEISURE": "문화", "BEAUTY": "뷰티"}


def _to_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _channel(cs_type: str) -> str:
    s = (cs_type or "").upper()
    for key, name in _CH_RULES:
        if key in s:
            return name
    return ""


def _category(recruit_type: str, ct_type: str) -> str:
    rt = (recruit_type or "").lower()
    if rt == "reporting":
        return "기자단"
    if rt in ("shipping", "buyreview"):
        return "배송"
    return _CT_CAT.get((ct_type or "").upper(), "")   # visiting: CT_type 으로, 미상은 제목분류


def _dday(x: dict) -> Optional[int]:
    c = _to_int(x.get("C_regi_end_date_count"))
    if c is not None:
        return c
    d = x.get("C_regi_end_date")
    if d:
        try:
            return (datetime.date.fromisoformat(str(d)[:10]) - datetime.date.today()).days
        except ValueError:
            pass
    return None


def _to_campaign(x: dict) -> Optional[Campaign]:
    cid = x.get("C_idx")
    if cid is None:
        return None
    title = (x.get("C_title") or "").strip()
    if not title:
        return None
    # 제목 앞 '[강원특별자치도/속초시]' 의 '/' → 공백(지역 정규화 인식용)
    title = re.sub(r"^\[([^\]]+)\]", lambda m: "[" + m.group(1).replace("/", " ") + "]", title)
    sub = (x.get("C_provision") or "").strip()
    full = (title + (f" {sub}" if sub else "")).strip()
    recruit = _to_int(x.get("C_choice_count"))
    applicants = _to_int(x.get("C_volunteer_count"))
    competition = round(applicants / recruit, 1) if recruit and applicants is not None else None
    return Campaign(
        site="popomon", site_name="포포몬", cid=str(cid), title=full,
        url=f"{BASE}/campaign/{cid}", region="",
        category=_category(x.get("C_recruit_type"), x.get("CT_type")),
        channel=_channel(x.get("CS_type")), dday=_dday(x),
        applicants=applicants, recruit=recruit, competition=competition,
        image=(x.get("thumb_img") or x.get("C_thumb_img_path") or ""),
    )


class PopomonAdapter(BaseAdapter):
    key = "popomon"
    name = "포포몬"
    enabled = True
    prunable = True            # 활성 전체를 매번 완주 → 소스에서 내려간 건 자동 삭제 가능
    min_interval = 1800        # 캠페인이 많아 별도 주기(30분)로만 수집

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        headers = {"User-Agent": UA, "Referer": BASE + "/campaign", "Accept": "application/json"}
        now = time.time()  # noqa: F841 (참고용; 상태는 C_state 로 판단)
        log.info("[popomon] 수집 시작...")
        for off in range(0, MAX_PAGES * PAGE, PAGE):
            try:
                r = await client.get(LIST.format(off=off), headers=headers, timeout=20.0)
            except Exception as e:
                log.warning("[popomon] pageNum=%d 실패: %s", off, e)
                break
            if r.status_code != 200:
                break
            data = ((r.json() or {}).get("data") or {}).get("contentsData") or []
            if not data:
                break
            page_cs = [c for c in (_to_campaign(x) for x in data) if c and c.cid not in seen]
            for c in page_cs:
                seen.add(c.cid)
            out.extend(page_cs)
            if on_page and page_cs:
                on_page(page_cs)
            if off and off % (PAGE * 50) == 0:
                log.info("[popomon] ... 누적 %d건", len(out))
            await asyncio.sleep(0.2)
        log.info("[popomon] 수집 완료 (총 %d건)", len(out))
        return out
