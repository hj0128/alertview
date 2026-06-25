"""어댑터 레지스트리. config.DEMO 여부에 따라 활성 어댑터 목록을 만든다."""
from __future__ import annotations
from typing import List

from .base import BaseAdapter, Campaign  # noqa: F401 (재노출)
from .dinnerqueen import DinnerQueenAdapter
from .nollawa import NollawaAdapter
from .reviewplace import ReviewPlaceAdapter
from .mrblog import MrblogAdapter
from .reviewnote import ReviewNoteAdapter
from .gangnam import GangnamAdapter
from .demo import DemoAdapter
from .stubs import RevuAdapter

ALL_ADAPTERS = [
    DinnerQueenAdapter(),
    NollawaAdapter(),
    ReviewPlaceAdapter(),
    MrblogAdapter(),
    ReviewNoteAdapter(),
    GangnamAdapter(),
    RevuAdapter(),
]


def active_adapters(demo: bool) -> List[BaseAdapter]:
    if demo:
        return [DemoAdapter()]
    return [a for a in ALL_ADAPTERS if a.enabled]
