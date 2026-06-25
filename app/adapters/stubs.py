"""아직 미구현 사이트들의 자리표시 어댑터.

레뷰/리뷰노트/리뷰플레이스/미블/놀러와체험단.
- 레뷰 등은 JS 렌더링(SPA)이라 단순 HTML 파싱으로는 비어 나옴 → 별도 작업 필요
  (내부 JSON API 발견 시 그 엔드포인트 호출 / 또는 헤드리스 브라우저).
구조만 잡아두고 fetch() 는 빈 리스트를 반환한다. 구현되면 enabled=True 로.
"""
from __future__ import annotations
from typing import List
import httpx
from .base import BaseAdapter, Campaign


class _Stub(BaseAdapter):
    enabled = False

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        return []


class RevuAdapter(_Stub):
    key, name = "revu", "레뷰"


class ReviewNoteAdapter(_Stub):
    key, name = "reviewnote", "리뷰노트"


class ReviewPlaceAdapter(_Stub):
    key, name = "reviewplace", "리뷰플레이스"


class MrblogAdapter(_Stub):
    key, name = "mrblog", "미블"


class NollawaAdapter(_Stub):
    key, name = "nollawa", "놀러와체험단"
