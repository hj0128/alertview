"""미블(mrblog.net) 어댑터.

주의: 캠페인 목록 페이지(/campaigns ...)는 로그인이 필요해 비로그인으로는 접근 불가.
따라서 공개된 홈페이지(SSR)에 노출되는 캠페인(시선집중·프리미엄·추천·마감임박, 약 30건)만 수집한다.
카드 구조: a.campaign_item > .thumb img / .area(채널라벨+아이콘+지역) / .subject(업체) / .desc / .d_day / .count
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from .. import config
from .base import BaseAdapter, Campaign, guess_in, CATEGORIES, UA

BASE = "https://www.mrblog.net"
# 로그인 후 무한스크롤 API(JSON {count, html}). page=N, count==0 이면 끝.
XHR = (BASE + "/xhr/campaigns?type=all&page={page}&category=&order_by=1"
       "&is_instagram%5B0%5D=0&is_instagram%5B1%5D=1&category_seq=")
_CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
_ID_RE = re.compile(r"/campaigns/(\d+)")
_DAY_RE = re.compile(r"(\d+)\s*일\s*남음")
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*명\s*/\s*모집\s*([\d,]+)\s*명")
_AREA_SKIP = {"배송", "배달", "기자단", "구매평", "온라인", ""}
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _parse(html: str) -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select("a.campaign_item"):
        m = _ID_RE.search(a.get("href", ""))
        if not m:
            continue
        cid = m.group(1)
        if cid in seen:
            continue
        subj_el = a.select_one(".subject")
        subject = subj_el.get_text(" ", strip=True) if subj_el else ""
        if not subject:
            continue
        seen.add(cid)

        desc_el = a.select_one(".desc")
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""

        # 채널 + 지역: .area 안의 라벨/아이콘 span 으로 채널 판별, 나머지 텍스트가 지역
        channel, region = "블로그", ""
        area_el = a.select_one(".area")
        if area_el:
            tokens = set()
            for sp in area_el.find_all("span"):
                tokens.update(sp.get("class", []) or [])
            if "reels" in tokens:
                channel = "릴스"
            elif "clip" in tokens:
                channel = "클립"
            elif "blog" in tokens:
                channel = "블로그"
            elif "insta" in tokens:
                channel = "인스타"
            for sp in area_el.find_all("span"):
                sp.extract()
            region = re.sub(r"\s+", " ", area_el.get_text(" ", strip=True)).strip()
            if region in _AREA_SKIP:
                region = ""

        dday_el = a.select_one(".d_day")
        dday_txt = dday_el.get_text(" ", strip=True) if dday_el else ""
        if re.search(r"D-?Day", dday_txt, re.I) or "오늘마감" in dday_txt:
            dday = 0
        elif (dm := _DAY_RE.search(dday_txt)):
            dday = _to_int(dm.group(1))
        else:
            dday = None

        cnt_el = a.select_one(".count")
        cnt = cnt_el.get_text(" ", strip=True) if cnt_el else ""
        applicants = recruit = competition = None
        if (am := _APPLY_RE.search(cnt)):
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)

        img_el = a.select_one("img")
        image = ""
        if img_el:
            image = (img_el.get("src") or img_el.get("data-src") or "").strip()
            if image.startswith("//"):
                image = "https:" + image

        short_desc = (desc[:40] + "…") if len(desc) > 41 else desc
        title = (f"[{region}] " if region else "") + subject + (f" {short_desc}" if short_desc else "")
        out.append(Campaign(
            site="mrblog", site_name="미블", cid=cid, title=title.strip(),
            url=f"{BASE}/campaigns/{cid}",
            region=region, category=guess_in(subject + " " + desc, CATEGORIES),
            channel=channel,
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class MrblogAdapter(BaseAdapter):
    key = "mrblog"
    name = "미블"
    enabled = True
    cookie_expired = False     # 폴러가 읽어 관리자에게 만료 알림을 보낸다
    partial = False            # 부분 수집(쿠키만료 폴백 등) → 폴러가 백필 완료로 찍지 않음

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        """MRBLOG_COOKIE 가 있으면 로그인 세션으로 전체 목록을, 없으면 홈 공개분만 수집."""
        self.cookie_expired = False
        self.partial = False
        if config.MRBLOG_COOKIE:
            items = await self._fetch_auth(client, on_page)
            if items is not None:
                return items
            # None = 쿠키 만료/실패 → 홈 폴백(전체가 아니므로 백필 미완료로 표시)
            self.partial = True
        return await self._fetch_home(client, on_page)

    async def _fetch_home(self, client, on_page) -> List[Campaign]:
        log.info("[mrblog] 수집 시작(홈 공개분)...")
        try:
            html = await self.get(client, BASE + "/")
        except Exception as e:
            log.warning("[mrblog] 홈 요청 실패: %s", e)
            return []
        items = _parse(html)
        if on_page and items:
            on_page(items)
        log.info("[mrblog] 수집 완료 (%d건, 홈 공개분)", len(items))
        return items

    async def _fetch_auth(self, client, on_page):
        """로그인 세션 쿠키로 /xhr/campaigns 전체 페이징. 실패 시 None 반환(→홈 폴백)."""
        cookie = config.MRBLOG_COOKIE
        base_h = {"User-Agent": UA, "Cookie": cookie}
        try:
            r = await client.get(BASE + "/campaigns", headers=base_h, timeout=20.0)
            if "/login" in str(r.url):
                log.warning("[mrblog] 세션 쿠키 만료/무효 → 홈 공개분으로 폴백 "
                            "(.env 의 MRBLOG_COOKIE 를 새로 로그인한 값으로 갱신하세요)")
                self.cookie_expired = True
                return None
            mm = _CSRF_RE.search(r.text)
            if not mm:
                log.warning("[mrblog] CSRF 토큰을 찾지 못함 → 홈 폴백 (쿠키 갱신 필요)")
                self.cookie_expired = True
                return None
            csrf = mm.group(1)
        except Exception as e:
            log.warning("[mrblog] 인증 초기화 실패(%s) → 홈 폴백", e)
            return None

        headers = {**base_h, "X-Requested-With": "XMLHttpRequest",
                   "X-CSRF-TOKEN": csrf, "Referer": BASE + "/campaigns"}
        cap = config.DQ_MAX_PAGES if config.DQ_MAX_PAGES > 0 else 500
        out, seen = [], set()
        log.info("[mrblog] 수집 시작(로그인 세션, 전체 목록)...")
        for page in range(1, cap + 1):
            try:
                resp = await client.get(XHR.format(page=page), headers=headers, timeout=20.0)
                data = resp.json()
            except Exception as e:
                # 부분 수집을 '완료'로 오인하지 않도록 표시(다음 수집에서 전체 재시도)
                log.warning("[mrblog] page=%d 요청 실패(중단): %s", page, e)
                self.partial = True
                break
            if not data.get("count"):
                break                          # 마지막 페이지
            page_new = [c for c in _parse(data.get("html") or "") if c.cid not in seen]
            for c in page_new:
                seen.add(c.cid)
            out.extend(page_new)
            keep = True
            if on_page and page_new:
                keep = on_page(page_new)
            log.info("[mrblog] page=%d +%d건 (누적 %d건)", page, len(page_new), len(out))
            if not page_new:
                break
            if keep is False:
                break
            await asyncio.sleep(0.3)
        log.info("[mrblog] 수집 완료 (%d건, 로그인 세션)", len(out))
        return out
