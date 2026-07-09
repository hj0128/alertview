"""리뷰노트(reviewnote.co.kr) 어댑터.

공개 JSON API 를 페이징해 전체 캠페인을 수집한다(로그인 불필요).
  GET /api/v2/campaigns?s=new&page=N  →  {page, objects, has_more, total_pages, total_count}
  s=new 정렬 = 최신순(page=0 이 최신). 16건/페이지.
객체 필드: id, title, offer, channel, sort, city(시/도), sido.name(시군구),
          applicantCount, infNum, applyEndAt(ISO), imageKey, category.title, isPremium
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import List, Optional
from urllib.parse import quote

import httpx

from .. import config
from .base import BaseAdapter, Campaign, UA

BASE = "https://www.reviewnote.co.kr"
API = BASE + "/api/v2/campaigns?s=new&page={page}"
_CH = {"BLOG": "블로그", "REELS": "릴스", "CLIP": "클립",
       "INSTAGRAM": "인스타", "SHORTS": "유튜브", "YOUTUBE": "유튜브"}
_FB = "https://firebasestorage.googleapis.com/v0/b/reviewnote-e92d9.appspot.com/o/{key}?alt=media"
log = logging.getLogger(__name__)


def _dday_from(iso: str) -> Optional[int]:
    if not iso:
        return None
    try:
        d = (datetime.date.fromisoformat(iso[:10]) - datetime.date.today()).days
    except (ValueError, TypeError):
        return None
    return d if d >= 0 else None


def _to_campaign(o: dict) -> Optional[Campaign]:
    cid = str(o.get("id") or "").strip()
    title0 = (o.get("title") or "").strip()
    if not cid or not title0:
        return None
    offer = (o.get("offer") or "").strip()
    short = (offer[:40] + "…") if len(offer) > 41 else offer

    city = (o.get("city") or "").strip()
    gu = ((o.get("sido") or {}).get("name") or "").strip()
    region = "" if city in ("", "재택", "온라인") else (city + (" " + gu if gu else "")).strip()

    sort = o.get("sort")
    cat = ((o.get("category") or {}).get("title") or "").strip()
    # 소스는 2축(유형 sort × 주제 category). 유형이 우리 8종의 '유형 칸'을 정하면 그것을 우선,
    # 아니면(방문형/당일지급) 주제로 매핑한다.
    #   sort: VISIT=방문형 TAKEOUT=구매형 DELIVERY=배송형 REPORTER=기자단
    #         PLATFORM_REPORTER=플랫폼기자단 TODAY=당일지급 ETC=포장 PAYBACK=페이백
    # 방문형/당일지급의 주제 매핑(배송 아님): 식품=음식관련→맛집, 반려동물=애견카페/체험→여가, 디지털=칸없음→기타
    _TOPIC = {"맛집": "맛집", "뷰티": "뷰티", "여행": "여가",
              "식품": "맛집", "반려동물": "여가", "디지털": "기타", "기타": "기타"}
    if sort == "PAYBACK" or o.get("paybackPlatform"):
        category = "페이백"
    elif sort in ("REPORTER", "PLATFORM_REPORTER"):
        category = "기자단"
    elif sort == "ETC":                      # ETC = 포장(소스 라벨)
        category = "포장"
    elif sort in ("DELIVERY", "TAKEOUT"):    # 배송형 / 구매형(제품 구매)
        category = "배송"
    else:                                    # VISIT(방문형) / TODAY(당일지급) → 주제로
        category = _TOPIC.get(cat, "기타")

    applicants = o.get("applicantCount")
    recruit = o.get("infNum")
    competition = round(applicants / recruit, 1) if (applicants is not None and recruit) else None

    ik = (o.get("imageKey") or "").strip()
    image = _FB.format(key=quote(ik, safe="")) if ik else ""

    title = (f"[{region}] " if region else "") + title0 + (f" {short}" if short else "")
    return Campaign(
        site="reviewnote", site_name="리뷰노트", cid=cid, title=title.strip(),
        url=f"{BASE}/campaigns/{cid}",
        region=region, category=category,
        channel=_CH.get(o.get("channel"), "블로그"),
        dday=_dday_from(o.get("applyEndAt")),
        applicants=applicants, recruit=recruit, competition=competition,
        image=image,
    )


class ReviewNoteAdapter(BaseAdapter):
    key = "reviewnote"
    name = "리뷰노트"
    enabled = True
    # 리뷰노트는 마감(조기마감 포함)되면 목록 API 에서 빠진다. 주기적 전체목록 대조로 정리
    # (poller._prune_reviewnote). 평소 증분 수집의 부분 정리는 비율 가드로 자동 보류된다.
    prunable = True

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        cap = config.DQ_MAX_PAGES if config.DQ_MAX_PAGES > 0 else 500
        # 이 API 는 Referer/Origin 이 없으면 403(빈 배열) 을 준다.
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": BASE + "/campaigns",
            "Origin": BASE,
        }
        log.info("[reviewnote] 수집 시작...")
        for page in range(0, cap):
            try:
                r = await client.get(API.format(page=page), headers=headers, timeout=20.0)
                r.raise_for_status()
                j = r.json()
            except Exception as e:
                # 부분 수집을 '완료'로 오인하지 않도록 전파(다음 수집에서 재시도)
                log.warning("[reviewnote] page=%d 요청 실패(중단): %s", page, e)
                raise
            d = j.get("data") if isinstance(j.get("data"), dict) else j
            objs = d.get("objects") or []
            if not objs:
                break
            page_new = []
            for o in objs:
                c = _to_campaign(o)
                if c and c.cid not in seen:
                    seen.add(c.cid)
                    page_new.append(c)
            out.extend(page_new)
            keep = True
            if on_page and page_new:
                keep = on_page(page_new)        # 최신순이라 새 항목 없으면 조기 종료 안전
            log.info("[reviewnote] page=%d +%d건 (누적 %d건)", page, len(page_new), len(out))
            if not d.get("has_more"):
                break
            if keep is False:
                break
            await asyncio.sleep(0.3)
        log.info("[reviewnote] 수집 완료 (총 %d건)", len(out))
        return out
