"""캠페인 ↔ 사용자 필터 매칭 규칙.

필터 종류 (각각 '설정돼 있을 때만' 적용, 서로는 AND):
  - sites     : 선택한 체험단(사이트)만 통과 (OR)  예: dinnerqueen, nollawa
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

# 카테고리 동의어 사전: 사이트가 category 를 안 주거나(놀러와 등) 제각각이라('여행'·'식품'),
# '맛집' 글자 그대로만 찾으면 실제 맛집(이자카야·오마카세·파스타…)이 다 누락된다.
# → 제목/태그/사이트분류 텍스트에서 아래 키워드 중 하나라도 있으면 그 카테고리로 인정.
# (오인 방지: '회','바' 처럼 1글자 모호 키워드는 넣지 않음 - '1회권' 등에 잘못 걸림)
CATEGORY_KEYWORDS = {
    "맛집": ["맛집", "식당", "레스토랑", "다이닝", "오마카세", "디너", "코스요리", "한우", "고기",
           "삼겹", "곱창", "족발", "보쌈", "횟집", "물회", "초밥", "스시", "사시미", "파스타",
           "뇨끼", "피자", "피제리아", "리조또", "스테이크", "브런치", "뷔페", "한식", "중식",
           "일식", "양식", "분식", "돈까스", "경양식", "이자카야", "포차", "술집", "혼술",
           "칵테일", "위스키", "와인", "전통주", "생맥주", "맥주", "안주", "라운지", "라멘",
           "우동", "국밥", "찌개", "전골", "치킨", "떡볶이", "김밥", "햄버거", "버거", "샐러드",
           "샌드위치", "카페", "베이커리", "디저트", "케이크", "다과", "푸드", "음식", "요리"],
    "뷰티": ["뷰티", "미용실", "미용", "헤어", "염색", "네일", "피부", "에스테틱", "스킨", "살롱",
           "마사지", "스파", "화장품", "코스메틱", "메이크업", "왁싱", "속눈썹", "래쉬", "태닝",
           "반영구", "제모", "경락", "두피", "탈모", "다이어트", "체형", "윤곽", "바디케어",
           "아로마", "피부관리", "눈썹"],
    "여가": ["여가", "여행", "숙박", "호텔", "펜션", "리조트", "풀빌라", "글램핑", "캠핑", "투어",
           "나들이", "체험", "클래스", "공방", "원데이", "공연", "전시", "뮤지컬", "키즈",
           "놀이", "액티비티", "레저", "방탈출", "스튜디오"],
    "배송": ["배송", "택배", "제품", "식품", "집으로"],
    "배달": ["배달"],
    "페이백": ["페이백", "캐시백", "리워드", "적립"],
    "기자단": ["기자단"],
}


def _category_hit(text: str, selected: List[str]) -> bool:
    """선택한 카테고리 중 하나라도 텍스트에 (동의어 포함) 걸리면 True."""
    for sel in selected:
        for kw in CATEGORY_KEYWORDS.get(sel, [sel]):
            if kw in text:
                return True
    return False


def matches(
    c: Campaign,
    keywords: List[str] = (),
    regions: List[str] = (),
    categories: List[str] = (),
    channels: List[str] = (),
    max_competition: Optional[float] = None,
    max_dday: Optional[int] = None,
    sites: List[str] = (),
) -> bool:
    if sites and c.site not in sites:
        return False
    text = c.text_for_match()
    low = text.lower()
    if keywords and not any(k.lower() in low for k in keywords):
        return False
    # 지역은 제목이 아니라 region 필드로만 매칭(예: 제목 '더기타레슨'이 '기타' 지역에 안 걸리게).
    # 시/도 선택('서울')은 '서울 송파구' 같은 하위까지 포함, 구 선택은 정확히 일치.
    if regions:
        reg = c.region or ""
        if not any(reg == r or reg.startswith(r + " ") for r in regions):
            return False
    if categories and not _category_hit(text, categories):
        return False
    if channels and not any(ch in text for ch in channels):
        return False
    if max_competition is not None and c.competition is not None and c.competition > max_competition:
        return False
    if max_dday is not None and c.dday is not None and c.dday > max_dday:
        return False
    return True
