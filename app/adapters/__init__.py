"""어댑터 레지스트리. config.DEMO 여부에 따라 활성 어댑터 목록을 만든다."""
from __future__ import annotations
from typing import List

from .base import BaseAdapter, Campaign  # noqa: F401 (재노출)
from .dinnerqueen import DinnerQueenAdapter
from .demo import DemoAdapter
from .stubs import (
    RevuAdapter, ReviewNoteAdapter, ReviewPlaceAdapter,
    MrblogAdapter, NollawaAdapter,
)

ALL_ADAPTERS = [
    DinnerQueenAdapter(),
    RevuAdapter(),
    ReviewNoteAdapter(),
    ReviewPlaceAdapter(),
    MrblogAdapter(),
    NollawaAdapter(),
]


def active_adapters(demo: bool) -> List[BaseAdapter]:
    if demo:
        return [DemoAdapter()]
    return [a for a in ALL_ADAPTERS if a.enabled]
