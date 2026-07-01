"""리뷰플레이스(reviewplace.co.kr) 어댑터.

목록은 SPA라 HTML에 바로 안 나오고, 내부 AJAX 엔드포인트가 카드 HTML을 돌려준다.
  POST /theme/rp/_ajax_cmp_list_tpl.php
  body: device=pc & type=<유형> & startnum=<offset> & endnum=<개수>
  유형: cmp_local(지역) / cmp_delivery(제품) / cmp_doc(기자단) / cmp_gm(구매평)
응답 카드(.item): 링크 /pr/?id=NNN, .tit(제목), .date(D-day), .num(신청/모집), img(썸네일)
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from .. import config
from .base import (BaseAdapter, Campaign, guess_in, CATEGORIES, CHANNELS,
                   REGIONS, UA)

BASE = "https://www.reviewplace.co.kr"
AJAX = f"{BASE}/theme/rp/_ajax_cmp_list_tpl.php"
# (목록 유형, 기본 카테고리)
# 소스 세부 카테고리(ct1/ct2)를 그대로 가져와 우리 8종으로 매핑. (type, ct1, ct2, 우리카테고리)
# ct2=None 이면 그 그룹 전체(세부 불필요). 프리미엄/N인플루언서는 현재 사이트에서 비활성이라 제외.
SUBCATS = [
    ("cmp_local", "지역", "맛집", "맛집"),
    ("cmp_local", "지역", "카페/베이커리", "맛집"),
    ("cmp_local", "지역", "뷰티/건강", "뷰티"),
    ("cmp_local", "지역", "운동/스포츠", "여가"),
    ("cmp_local", "지역", "숙박", "여가"),
    ("cmp_local", "지역", "문화/체험", "여가"),
    ("cmp_local", "지역", "생활/편의", "기타"),
    ("cmp_local", "지역", "시크릿쇼퍼", "기타"),
    ("cmp_local", "지역", "기타", "기타"),
    ("cmp_delivery", "제품", "뷰티", "배송"),
    ("cmp_delivery", "제품", "식품", "배송"),
    ("cmp_delivery", "제품", "생활", "배송"),
    ("cmp_delivery", "제품", "유아동", "배송"),
    ("cmp_delivery", "제품", "운동/건강", "배송"),
    ("cmp_delivery", "제품", "디지털", "배송"),
    ("cmp_delivery", "제품", "패션/잡화", "배송"),
    ("cmp_delivery", "제품", "반려동물", "배송"),
    ("cmp_delivery", "제품", "도서/교육", "배송"),
    ("cmp_delivery", "제품", "서비스", "기타"),
    ("cmp_delivery", "제품", "기타", "배송"),
    ("cmp_doc", None, None, "기자단"),
    ("cmp_gm", None, None, "배송"),
]
PAGE = 30                      # 1회 요청 개수(startnum 증가 단위)
_ID_RE = re.compile(r"id=(\d+)")
_DDAY_RE = re.compile(r"D\s*-\s*(\d+)")
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*/\s*([\d,]+)\s*명")
_IMG_ATTRS = ["data-src", "data-original", "src"]
_CH_WORDS = {"인스타", "릴스", "클립", "쇼츠", "숏폼", "숏츠", "블로그", "유튜브", "홀"}
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _region(title: str) -> str:
    """제목 첫 대괄호 안의 '/'로 구분된 토큰에서 시/도(+구)를 추출.
    예: '[릴스/인천/남동구] ...' → '인천 남동구', '[서울/송파] ...' → '서울 송파'."""
    m = re.match(r"\s*\[([^\]]+)\]", title)
    if not m:
        return ""
    parts = [p.strip() for p in m.group(1).split("/") if p.strip()]
    for i, p in enumerate(parts):
        sido = next((r for r in REGIONS if p == r or p.startswith(r)), None)
        if sido:
            gu = ""
            if i + 1 < len(parts):
                nxt = parts[i + 1].split()[0] if parts[i + 1].split() else ""
                if nxt and nxt not in _CH_WORDS and not re.search(r"\d", nxt) and len(nxt) <= 5:
                    gu = nxt
            return (sido + (" " + gu if gu else "")).strip()
    return ""


def _pick_img(it) -> str:
    for img in it.find_all("img"):
        for attr in _IMG_ATTRS:
            v = (img.get(attr) or "").strip()
            if v.startswith("//"):
                v = "https:" + v
            if v.startswith("http") and "data:image" not in v:
                return v
    return ""


def _parse(html: str, default_cat: str) -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for it in soup.select(".item"):
        a = it.select_one('a[href*="/pr/"]') or it.find("a", href=True)
        if not a:
            continue
        m = _ID_RE.search(a.get("href", ""))
        if not m:
            continue
        cid = m.group(1)
        tit_el = it.select_one(".tit")
        title = tit_el.get_text(" ", strip=True) if tit_el else ""
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue

        date_txt = it.select_one(".date").get_text(" ", strip=True) if it.select_one(".date") else ""
        num_txt = it.select_one(".num").get_text(" ", strip=True) if it.select_one(".num") else ""

        if "오늘마감" in date_txt:
            dday = 0
        elif (dm := _DDAY_RE.search(date_txt)):
            dday = _to_int(dm.group(1))
        else:
            dday = None

        applicants = recruit = competition = None
        if (am := _APPLY_RE.search(num_txt)):
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)

        out.append(Campaign(
            site="reviewplace", site_name="리뷰플레이스", cid=cid, title=title,
            url=f"{BASE}/pr/?id={cid}",
            region=_region(title),
            category=default_cat,
            channel=guess_in(title, CHANNELS) or "블로그",
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=_pick_img(it), extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class ReviewPlaceAdapter(BaseAdapter):
    prunable = True
    key = "reviewplace"
    name = "리뷰플레이스"
    enabled = True

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        cap = config.DQ_MAX_PAGES if config.DQ_MAX_PAGES > 0 else 500
        headers = {
            "User-Agent": UA,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + "/",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        log.info("[reviewplace] 수집 시작...")
        try:                                   # 세션 쿠키 워밍업
            await client.get(BASE + "/", headers={"User-Agent": UA}, timeout=20.0)
        except Exception as e:
            log.warning("[reviewplace] 초기 GET 실패(무시): %s", e)

        for typ, ct1, ct2, cat in SUBCATS:
            start = 0
            for _ in range(cap):
                body = {"device": "pc", "type": typ, "startnum": start, "endnum": PAGE}
                if ct1:
                    body["ct1"] = ct1
                if ct2:
                    body["ct2"] = ct2
                try:
                    resp = await client.post(AJAX, data=body, headers=headers, timeout=20.0)
                    resp.raise_for_status()
                    html = resp.text
                except Exception as e:
                    log.warning("[reviewplace] %s start=%d 요청 실패(중단): %s", typ, start, e)
                    raise
                page_new = [c for c in _parse(html, cat) if c.cid not in seen]
                for c in page_new:
                    seen.add(c.cid)
                out.extend(page_new)
                keep = True
                if on_page and page_new:
                    keep = on_page(page_new)
                log.info("[reviewplace] %s/%s start=%d +%d (누적 %d)", ct1 or typ, ct2 or "-", start, len(page_new), len(out))
                if not page_new:
                    break
                if keep is False:
                    break
                start += PAGE
                await asyncio.sleep(0.3)
        log.info("[reviewplace] 수집 완료 (총 %d건)", len(out))
        return out
