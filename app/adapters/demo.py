"""데모 어댑터 — 실제 사이트 없이 파이프라인 검증. DEMO=1 일 때만 등록."""
from __future__ import annotations
import random
import time
from typing import List

import httpx
from .base import BaseAdapter, Campaign

_REGIONS = ["서울 강남", "경기 수원", "부산 해운대", "대전 유성", "강원 횡성"]
_ITEMS = ["수제버거 무한리필", "감성카페 디저트", "한우 오마카세", "요가 클래스"]
_CATS = ["맛집", "배송", "여가", "뷰티"]
_CHANS = ["블로그", "릴스", "클립"]


class DemoAdapter(BaseAdapter):
    key = "demo"
    name = "데모"

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        cid = str(int(time.time()))
        region = random.choice(_REGIONS)
        applicants, recruit = random.randint(0, 10), random.randint(1, 5)
        items = [Campaign(
            site=self.key, site_name=self.name, cid=cid,
            title=f"[{region}][{random.choice(_CHANS)}] {random.choice(_ITEMS)}",
            url="https://example.com/demo/" + cid,
            region=region, category=random.choice(_CATS), channel=random.choice(_CHANS),
            dday=random.randint(1, 9), applicants=applicants, recruit=recruit,
            competition=round(applicants / recruit, 1),
        )]
        if on_page:
            on_page(items)
        return items
