"""캠페인 ↔ 사용자 필터 매칭 규칙.

필터 종류 (각각 '설정돼 있을 때만' 적용, 서로는 AND):
  - keywords  : 하나라도 캠페인 텍스트에 포함되면 통과 (OR)
  - regions   : 하나라도 포함되면 통과 (OR)
  - categories: 하나라도 일치하면 통과 (OR)  예: 맛집, 카페
  - channels  : 하나라도 일치하면 통과 (OR)  예: 블로그, 릴스
  - max_competition: 경쟁률 이하만 (당첨확률↑). 경쟁률 미상이면 제외하지 않음.
  - max_dday  : 마감까지 남은 일수 이하만. 미상이면 제외하지 않음.
모든 필터가 비어 있으면 = 모든 신규 캠페인 수신.
"""
from __future__ import annotations
from typing import List, Optional
from .adapters.base import Campaign


def matches(
    c: Campaign,
    keywords: List[str] = (),
    regions: List[str] = (),
    categories: List[str] = (),
    channels: List[str] = (),
    max_competition: Optional[float] = None,
    max_dday: Optional[int] = None,
) -> bool:
    text = c.text_for_match()
    low = text.lower()
    if keywords and not any(k.lower() in low for k in keywords):
        return False
    if regions and not any(r in text for r in regions):
        return False
    if categories and not any(cat in text for cat in categories):
        return False
    if channels and not any(ch in text for ch in channels):
        return False
    if max_competition is not None and c.competition is not None and c.competition > max_competition:
        return False
    if max_dday is not None and c.dday is not None and c.dday > max_dday:
        return False
    return True
