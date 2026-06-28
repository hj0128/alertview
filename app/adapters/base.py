"""사이트 어댑터 공통 인터페이스."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import List, Optional

import httpx

log = logging.getLogger(__name__)

REGIONS = [
    "서울", "경기", "인천", "부산", "대구", "대전", "광주", "울산", "세종",
    "강원", "충북", "충남", "충청", "전북", "전남", "전라", "경북", "경남",
    "제주", "전국",
]

# 분류 키워드 (제목/카드 텍스트에서 인식)
CHANNELS = ["블로그", "클립", "인스타", "릴스", "유튜브", "숏폼", "숏츠"]
CATEGORIES = ["맛집", "배송", "배달", "여가", "뷰티", "페이백", "기자단"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


@dataclass
class Campaign:
    site: str
    site_name: str
    cid: str
    title: str
    url: str
    region: str = ""
    category: str = ""
    channel: str = ""
    dday: Optional[int] = None          # 마감까지 남은 일수
    applicants: Optional[int] = None    # 신청자수
    recruit: Optional[int] = None       # 모집인원
    competition: Optional[float] = None # 경쟁률 = 신청/모집
    image: str = ""     # 썸네일 URL
    extra: str = ""

    def text_for_match(self) -> str:
        return " ".join([self.title, self.region, self.category, self.channel])


def guess_region(title: str) -> str:
    for token in re.findall(r"\[([^\]]+)\]", title):
        for r in REGIONS:
            if r in token:
                return token.strip()
    return ""


def guess_in(text: str, vocab: List[str]) -> str:
    for v in vocab:
        if v in text:
            return v
    return ""


class BaseAdapter:
    key: str = ""
    name: str = ""
    enabled: bool = True

    async def fetch(self, client: "httpx.AsyncClient", on_page=None) -> List[Campaign]:
        """on_page(list)->bool: 페이지 수집 즉시 호출. False 반환 시 조기 종료."""
        raise NotImplementedError

    async def get(self, client: "httpx.AsyncClient", url: str,
                  headers: Optional[dict] = None, retries: int = 3) -> str:
        """일시 오류(타임아웃·429·5xx 등)는 지수 백오프로 재시도. 최종 실패 시 예외 전파.
        호출부에서 예외를 삼키고 break 하면 부분 수집을 '완료'로 오인하므로, 끝까지 전파한다."""
        h = {"User-Agent": UA}
        if headers:
            h.update(headers)
        last_err: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                resp = await client.get(url, headers=h, timeout=20.0)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                last_err = e
                if attempt < retries:
                    await asyncio.sleep(0.5 * attempt)   # 0.5s, 1.0s ...
        raise last_err
