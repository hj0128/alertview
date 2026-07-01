"""어디야(odiya.kr) 어댑터. 링블 계열 CMS(구형 테이블 SSR)인데 마크업이 깨져 있어
BeautifulSoup 트리로는 대부분의 카드를 놓친다 → 원문(raw HTML)에서 정규식으로 추출한다.

목록: /category.php?category=ID&start=N  (start 68 단위 페이징)
  소스 카테고리: 829 맛집 / 832 체험 / 1016 제품 / 834 기자단 / 1018 프리미엄 / 1023 사진촬영만
상세: /detail.php?number=N
캠페인마다 number= 가 2번(썸네일/정보 칸) 나오며, 정보 칸 부근에 'D-N'·제목('[지역]...')·
'신청 X / 모집 Y' 가 모여 있다. 각 cid 의 '마지막 등장' 기준으로 구간을 잘라 파싱한다.
'마감'(종료)인 캠페인은 저장하지 않는다. 채널은 소스가 안 줘서 제목 키워드로 추정(기본 블로그).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import httpx

from .base import BaseAdapter, Campaign, guess_region, UA

BASE = "https://odiya.kr"
LIST = BASE + "/category.php?category={cat}&start={start}"
STEP = 68
MAX_PAGES = 40                       # 카테고리당 안전 상한(새 항목 없으면 그 전에 종료)
# 소스 카테고리 → 우리 표준. 빈값은 제목으로 분류(classify).
#   829 맛집(식당·식사권) / 832 체험(풀하우스·낚시·체험 등 여가) / 1016 제품(배송) /
#   834 기자단 / 1023 사진촬영(사진관→여가). 1013/1018(프리미엄)/1020 은 미상 → 제목분류.
CATS = {
    "829": "맛집", "1016": "배송", "834": "기자단",
    "832": "여가", "1023": "여가",
    "1013": "", "1018": "", "1020": "",
}
_NUM_RE = re.compile(r"number=(\d+)")
_DDAY_RE = re.compile(r"D-\s*(\d+)|(\d+)\s*일\s*남음")
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*/\s*모집\s*([\d,]+)")
_TITLE_CUT = re.compile(r"리뷰어신청|신청\s*[\d,]+|D-\s*\d|오늘\s*마감|마감")
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _strip(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;?", " ", s).replace("\xa0", " ")   # '&nbsp' 세미콜론 누락 케이스 포함
    return re.sub(r"\s+", " ", s).strip()


def _channel(title: str) -> str:
    if "인스타" in title:
        return "인스타"
    if "릴스" in title:
        return "릴스"
    if "유튜브" in title or "쇼츠" in title or "숏츠" in title:
        return "유튜브"
    if "클립" in title:
        return "클립"
    return "블로그"


_IMG_RE = re.compile(r'number=(\d+)[^>]*>\s*<img[^>]+src=["\']([^"\']+)["\']')


def _parse(html: str, category: str) -> List[Optional[Campaign]]:
    """반환: 각 cid 의 Campaign(활성) 또는 None(마감/정보없음). 페이징 종료 판정은 cid 수로."""
    imgs = {}
    for m in _IMG_RE.finditer(html):
        imgs.setdefault(m.group(1), m.group(2))
    last = {}
    for m in _NUM_RE.finditer(html):
        last[m.group(1)] = m.start()
    items = sorted(last.items(), key=lambda kv: kv[1])
    out: List[Optional[Campaign]] = []
    for i, (cid, pos) in enumerate(items):
        end = items[i + 1][1] if i + 1 < len(items) else pos + 1200
        seg = _strip(html[pos:end])
        if "신청" not in seg or "모집" not in seg:
            out.append(None)
            continue

        if (dm := _DDAY_RE.search(seg)):
            dday = _to_int(dm.group(1) or dm.group(2))
        elif "오늘마감" in seg or "오늘 마감" in seg:
            dday = 0
        elif "마감" in seg:
            out.append(None)                 # 종료된 캠페인 → 저장 안 함
            continue
        else:
            dday = None

        mb = re.search(r"\[[가-힣]", seg)        # '[경기...' 처럼 지역 대괄호로 시작하는 제목만(JS/검색박스 슬라이스 배제)
        if not mb:
            out.append(None)
            continue
        title = _TITLE_CUT.split(seg[mb.start():], 1)[0].strip()[:70]
        if len(title) < 4:
            out.append(None)
            continue

        applicants = recruit = competition = None
        if (am := _APPLY_RE.search(seg)):
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)

        src = imgs.get(cid, "")
        image = (BASE + src[1:] if src.startswith("./") else (BASE + src if src.startswith("/") else src)) if src else ""

        out.append(Campaign(
            site="odiya", site_name="어디야", cid=cid, title=title,
            url=f"{BASE}/detail.php?number={cid}",
            region=guess_region(title), category=category, channel=_channel(title),
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class OdiyaAdapter(BaseAdapter):
    key = "odiya"
    name = "어디야"
    enabled = True
    prunable = True          # 카테고리별 전체 목록 완주 → 소스에서 내려간 건 자동 삭제 가능

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        headers = {"User-Agent": UA, "Referer": BASE + "/"}
        log.info("[odiya] 수집 시작...")
        for cat, cname in CATS.items():
            n0 = len(out)
            for page in range(MAX_PAGES):
                try:
                    html = (await client.get(LIST.format(cat=cat, start=page * STEP),
                                             headers=headers, timeout=20.0)).text
                except Exception as e:
                    log.warning("[odiya] cat %s start %d 실패: %s", cat, page * STEP, e)
                    break
                # cid 단위 dedup: 새 cid 가 하나도 없으면 목록 끝
                ids = list(dict.fromkeys(_NUM_RE.findall(html)))
                fresh_ids = [c for c in ids if c not in seen]
                if not fresh_ids:
                    break
                camps = _parse(html, cname)
                page_cs = []
                for c in camps:
                    if c is None or c.cid in seen:
                        continue
                    seen.add(c.cid)
                    page_cs.append(c)
                for c in fresh_ids:              # 마감/정보없음 cid 도 seen 처리(페이징 종료용)
                    seen.add(c)
                out.extend(page_cs)
                if on_page and page_cs:
                    on_page(page_cs)
                await asyncio.sleep(0.3)
            log.info("[odiya] cat %s(%s) +%d건 (누적 %d건)", cat, cname or "분류", len(out) - n0, len(out))
        log.info("[odiya] 수집 완료 (총 %d건)", len(out))
        return out
