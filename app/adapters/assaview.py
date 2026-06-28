"""아싸뷰(assaview.co.kr) 어댑터. GnuBoard SSR.

목록: /campaign_list.php?orderby=cp_sdatetime&page=N (최신순, 20건/page)
카드(li > a[cp_id=N]): .subject(제목·지역 대괄호), .cp_type(유형), 채널 아이콘 img,
  마감일시(YYYY/MM/DD), '신청 X / Y명', 썸네일 ./data/campaign/thumb/...
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from .. import config
from .base import BaseAdapter, Campaign, guess_region, UA

BASE = "https://assaview.co.kr"
LIST = BASE + "/campaign_list.php?orderby=cp_sdatetime&page={page}"
_ID_RE = re.compile(r"cp_id=(\d+)")
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*/\s*([\d,]+)\s*명")
_DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _channel(li) -> str:
    icons = " ".join(i.get("src", "") for i in li.find_all("img"))
    if "reels_icon" in icons:
        return "릴스"
    if "clip_icon" in icons:
        return "클립"
    if "insta_icon" in icons:
        return "인스타"
    return "블로그"


def _abs(src: str) -> str:
    src = (src or "").strip()
    if src.startswith("./"):
        return BASE + src[1:]
    if src.startswith("/"):
        return BASE + src
    return src


def _parse(html: str) -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select('a[href*="cp_id="]'):
        m = _ID_RE.search(a.get("href", ""))
        if not m:
            continue
        cid = m.group(1)
        if cid in seen:
            continue
        li = a.find_parent("li") or a
        subject = (li.select_one(".subject").get_text(" ", strip=True)
                   if li.select_one(".subject") else "")
        subject = re.sub(r"\s+", " ", subject).strip()
        if not subject:
            continue
        seen.add(cid)

        txt = li.get_text(" ", strip=True)
        am = _APPLY_RE.search(txt)
        applicants = recruit = competition = None
        if am:
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)

        dday = None
        # 마감일시는 .imgBox 클래스 또는 카드 텍스트에 YYYY/MM/DD 로 들어있다
        src = (li.select_one(".imgBox").get("class") if li.select_one(".imgBox") else None)
        dsrc = " ".join(src) if src else txt
        if (dm := _DATE_RE.search(dsrc)):
            try:
                dl = datetime.date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
                dday = (dl - datetime.date.today()).days
                if dday < 0:
                    dday = None
            except ValueError:
                pass

        thumb = ""
        for img in li.find_all("img"):
            s = img.get("src") or img.get("data-src") or ""
            if "thumb" in s or "/campaign/" in s:
                thumb = _abs(s)
                break

        out.append(Campaign(
            site="assaview", site_name="아싸뷰", cid=cid, title=subject,
            url=f"{BASE}/campaign.php?cp_id={cid}",
            region=guess_region(subject), category="", channel=_channel(li),
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=thumb, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class AssaviewAdapter(BaseAdapter):
    key = "assaview"
    name = "아싸뷰"
    enabled = True

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        cap = config.DQ_MAX_PAGES if config.DQ_MAX_PAGES > 0 else 500
        headers = {"User-Agent": UA, "Referer": BASE + "/"}
        log.info("[assaview] 수집 시작...")
        for page in range(1, cap + 1):
            try:
                html = await self.get(client, LIST.format(page=page), headers=headers)
            except Exception as e:
                log.warning("[assaview] %d페이지 요청 실패(중단): %s", page, e)
                raise
            cards = _parse(html)
            if not cards:
                break
            page_new = [c for c in cards if c.cid not in seen]
            for c in page_new:
                seen.add(c.cid)
            out.extend(page_new)
            keep = True
            if on_page and page_new:
                keep = on_page(page_new)
            log.info("[assaview] %d페이지 +%d건 (누적 %d건)", page, len(page_new), len(out))
            if keep is False:
                break
            await asyncio.sleep(0.3)
        log.info("[assaview] 수집 완료 (총 %d건)", len(out))
        return out
