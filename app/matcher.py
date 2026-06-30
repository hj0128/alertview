"""캠페인 ↔ 사용자 필터 매칭 규칙.

필터 종류 (각각 '설정돼 있을 때만' 적용, 서로는 AND):
  - sites     : 선택한 체험단(사이트)만 통과 (OR)  예: dinnerqueen, nollawa
  - keywords  : 하나라도 캠페인 텍스트에 포함되면 통과 (OR)
  - regions   : 하나라도 포함되면 통과 (OR)
  - categories: 하나라도 일치하면 통과 (OR)  예: 맛집, 카페
  - channels  : 하나라도 일치하면 통과 (OR)  예: 블로그, 릴스
  - 경쟁률·마감일·모집수·지원수: 각각 min~max 범위(설정된 쪽만 적용). 값이 미상이면 제외하지 않음.
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
    # 포장(테이크아웃)은 맛집보다 우선 — '치킨 포장 체험권' 같은 건 포장으로 본다.
    # 일반 식당 텍스트('포장마차' 등)에 안 걸리도록 '포장 ~' 조합어만 키워드로 사용.
    "포장": ["테이크아웃", "테이크 아웃", "포장 체험", "포장체험", "포장 가능", "포장가능",
           "포장 주문", "포장주문", "포장 전용", "포장전용", "포장 할인", "포장 메뉴", "포장만"],
    "맛집": ["맛집", "식당", "레스토랑", "다이닝", "오마카세", "디너", "코스요리", "한우", "고기",
           "삼겹", "곱창", "족발", "보쌈", "횟집", "물회", "초밥", "스시", "사시미", "파스타",
           "뇨끼", "피자", "피제리아", "리조또", "스테이크", "브런치", "뷔페", "한식", "중식",
           "일식", "양식", "분식", "돈까스", "경양식", "이자카야", "포차", "술집", "혼술",
           "칵테일", "위스키", "와인", "전통주", "생맥주", "맥주", "안주", "라운지", "라멘",
           "우동", "국밥", "찌개", "전골", "치킨", "떡볶이", "김밥", "햄버거", "버거", "샐러드",
           "샌드위치", "카페", "베이커리", "디저트", "케이크", "다과", "푸드", "음식", "요리",
           "짬뽕", "짜장", "중화요리", "국수", "칼국수", "냉면", "막국수", "쌀국수", "백반",
           "한정식", "곰탕", "설렁탕", "해장국", "닭갈비", "갈비", "불고기", "삼계탕", "쭈꾸미",
           "막창", "대창", "곱창", "장어", "낙지", "해물", "조개", "덮밥", "비빔밥", "만두",
           "야키토리", "꼬치", "텐동", "규동", "초밥집", "포케", "쌈밥", "두부", "감자탕",
           "순대", "빵집", "제과", "곱창전골", "수육", "마라탕", "샤브", "전집", "막국수"],
    "뷰티": ["뷰티", "미용실", "미용", "헤어", "염색", "네일", "피부", "에스테틱", "스킨", "살롱",
           "마사지", "스파", "화장품", "코스메틱", "메이크업", "왁싱", "속눈썹", "래쉬", "태닝",
           "반영구", "제모", "경락", "두피", "탈모", "다이어트", "체형", "윤곽", "바디케어",
           "아로마", "피부관리", "눈썹"],
    # '체험'은 '체험권/체험단'이 모든 캠페인에 붙어 오분류를 유발하므로 키워드에서 제외.
    "여가": ["여가", "여행", "숙박", "호텔", "펜션", "리조트", "풀빌라", "글램핑", "캠핑", "투어",
           "나들이", "클래스", "공방", "원데이", "공연", "전시", "뮤지컬", "키즈",
           "놀이", "액티비티", "레저", "방탈출", "스튜디오", "볼링", "당구", "노래방",
           "스크린골프", "PC방", "만화카페", "사격", "양궁", "클라이밍", "요트", "서핑"],
    "배송": ["배송", "택배", "제품", "식품", "집으로"],
    "배달": ["배달"],
    "페이백": ["페이백", "캐시백", "리워드", "적립"],
    "기자단": ["기자단"],
}


# 표준 카테고리 집합(8종). 어댑터가 준 값이 이 안에 있으면 그대로 신뢰, 아니면 classify.
CANONICAL = set(CATEGORY_KEYWORDS) | {"기타"}


def classify(text: str) -> str:
    """텍스트를 표준 카테고리 하나로 분류. 어떤 키워드에도 안 걸리면 '기타'.
    (사전 순서가 우선순위 - 포장 > 맛집 > 뷰티 > 여가 > 배송 > 배달 > 페이백 > 기자단)"""
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in text for k in kws):
            return cat
    return "기타"


def category_of(stored: str, text: str) -> str:
    """캠페인의 표준 카테고리. 저장값(어댑터/DB)이 표준 8종이면 그대로 신뢰,
    아니면(빈값·'여행'·'식품'…) 텍스트로 분류. 표시·필터가 같은 기준을 쓰게 하는 단일 진실."""
    s = (stored or "").strip()
    return s if s in CANONICAL else classify(text)


# 알려진 콘텐츠 채널(이 중 어디에도 안 걸리면 '기타'로 본다).
KNOWN_CHANNELS = ["블로그", "클립", "인스타", "릴스", "유튜브", "숏폼", "숏츠", "틱톡"]


def _channel_hit(ch: str, text: str) -> bool:
    # '기타' = 알려진 채널 어디에도 안 걸리는 캠페인. 글자 '기타'(악기 등) 부분일치로 잡지 않는다.
    if ch == "기타":
        return not any(k in text for k in KNOWN_CHANNELS)
    return ch in text


def _in_range(v, lo, hi) -> bool:
    """값 v 가 [lo, hi] 범위 안인지. lo/hi 는 설정된 쪽만 적용(이상/이하).
    v 가 미상(None)이면 숫자필터로 제외하지 않는다(알림 누락 방지)."""
    if v is None:
        return True
    if lo is not None and v < lo:
        return False
    if hi is not None and v > hi:
        return False
    return True


def matches(
    c: Campaign,
    keywords: List[str] = (),
    regions: List[str] = (),
    categories: List[str] = (),
    channels: List[str] = (),
    max_competition: Optional[float] = None,
    max_dday: Optional[int] = None,
    sites: List[str] = (),
    min_recruit: Optional[int] = None,
    max_applicants: Optional[int] = None,
    min_competition: Optional[float] = None,
    min_dday: Optional[int] = None,
    max_recruit: Optional[int] = None,
    min_applicants: Optional[int] = None,
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
    if categories and category_of(c.category, text) not in categories:
        return False
    if channels and not any(_channel_hit(ch, text) for ch in channels):
        return False
    # 숫자 범위 필터(각각 min~max, 미상은 통과)
    if not _in_range(c.competition, min_competition, max_competition):
        return False
    if not _in_range(c.dday, min_dday, max_dday):
        return False
    if not _in_range(c.recruit, min_recruit, max_recruit):
        return False
    if not _in_range(c.applicants, min_applicants, max_applicants):
        return False
    return True


def matches_filter(c: Campaign, f: dict) -> bool:
    """사용자 필터 dict(get_all_filters 형식)로 매칭. 웹 피드·텔레그램 알림이 같은 규칙을 쓰도록 단일 진입점."""
    return matches(
        c, f["keywords"], f["regions"], f["categories"], f["channels"],
        sites=f.get("sites") or [],
        min_competition=f.get("min_competition"), max_competition=f.get("max_competition"),
        min_dday=f.get("min_dday"), max_dday=f.get("max_dday"),
        min_recruit=f.get("min_recruit"), max_recruit=f.get("max_recruit"),
        min_applicants=f.get("min_applicants"), max_applicants=f.get("max_applicants"),
    )
