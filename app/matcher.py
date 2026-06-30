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
    # 8종: 맛집 여가 뷰티 페이백 기자단 배송 포장 기타. 순서=우선순위(앞이 먼저).
    "포장": ["테이크아웃", "테이크 아웃", "포장 체험", "포장체험", "포장 가능", "포장가능",
           "포장 주문", "포장주문", "포장 전용", "포장전용", "포장 메뉴", "포장만", "방문포장"],
    "페이백": ["페이백", "캐시백", "리워드", "적립", "페이 백"],
    "기자단": ["기자단"],
    # 여가 = 숙박·문화·체험·스포츠·운동 등 '활동/경험'
    "여가": ["숙박", "호텔", "펜션", "리조트", "풀빌라", "글램핑", "오토캠핑", "캠핑", "게스트하우스",
           "모텔", "카라반", "독채", "민박", "한옥스테이", "료칸", "여행", "투어", "나들이",
           "공연", "전시", "뮤지컬", "연극", "콘서트", "영화", "박물관", "미술관", "갤러리",
           "공방", "원데이", "클래스", "도자기", "드로잉", "악기", "레슨", "전시회", "페스티벌",
           "보컬", "댄스", "발레", "피아노", "바이올린", "드럼", "음악학원", "미술학원", "노래교실",
           "영어", "화상영어", "원어민", "영어회화", "회화", "과외", "어학", "토익", "학습지", "강의", "수강",
           "헬스", "피트니스", "PT", "필라테스", "요가", "바레", "골프", "스크린골프", "볼링", "당구",
           "클라이밍", "서핑", "테니스", "수영", "배드민턴", "풋살", "축구", "야구", "농구", "스쿼시",
           "사격", "양궁", "승마", "스키", "스노보드", "요트", "카약", "복싱", "주짓수", "크로스핏",
           "암벽", "스케이트", "유도", "검도", "태권도", "합기도", "무에타이", "펜싱", "탁구",
           "키즈카페", "액티비티", "놀이공원", "테마파크", "아쿠아리움", "동물원", "실내놀이터",
           "트램폴린", "VR", "방방", "키즈", "원데이체험", "농장체험", "수목원", "방탈출", "방탈",
           "보드게임", "파티룸", "공간대여", "루프탑파티",
           "스튜디오", "사진관", "사진스튜디오", "셀프스튜디오", "셀프사진", "프로필사진",
           "스냅사진", "바디프로필", "증명사진", "가족사진", "만삭사진", "웨딩촬영", "포토부스",
           "네컷", "인생네컷", "포토이즘", "웨딩홀", "컨벤션", "웨딩", "예식", "결혼식"],
    # 뷰티 = 미용·피부·바디케어·마사지·다이어트
    "뷰티": ["뷰티", "미용실", "미용", "헤어", "염색", "네일", "왁싱", "속눈썹", "래쉬", "메이크업",
           "반영구", "눈썹", "젤네일", "페디큐어", "퍼스널컬러", "문신", "타투", "피부", "에스테틱",
           "스킨케어", "피부케어", "피부관리", "여드름", "여드름케어", "모공", "리프팅", "보톡스",
           "필러", "레이저", "점빼기", "슈가링", "각질", "필링", "화장품", "코스메틱", "살롱",
           "태닝", "제모", "두피케어", "헤어케어", "속눈썹펌", "마사지", "스파", "경락", "사우나",
           "찜질", "림프", "바디케어", "아로마", "다이어트", "체형", "교정", "도수", "탈모"],
    # 맛집 = 음식점 + 카페/디저트
    "맛집": ["맛집", "식당", "레스토랑", "다이닝", "오마카세", "디너", "코스요리", "한우", "고기",
           "삼겹", "곱창", "족발", "보쌈", "횟집", "물회", "초밥", "스시", "사시미", "파스타",
           "피자", "리조또", "스테이크", "뷔페", "한식", "중식", "일식", "양식", "분식", "돈까스",
           "이자카야", "포차", "술집", "혼술", "칵테일", "위스키", "와인", "전통주", "맥주", "안주",
           "라멘", "우동", "국밥", "찌개", "전골", "치킨", "떡볶이", "김밥", "햄버거", "버거",
           "샐러드", "샌드위치", "푸드", "음식", "요리", "짬뽕", "짜장", "중화요리", "국수", "칼국수",
           "냉면", "막국수", "쌀국수", "백반", "한정식", "곰탕", "설렁탕", "해장국", "닭갈비", "갈비",
           "불고기", "삼계탕", "쭈꾸미", "막창", "대창", "곱창전골", "장어", "낙지", "해물", "조개",
           "덮밥", "비빔밥", "만두", "야키토리", "꼬치", "텐동", "규동", "쌈밥", "두부", "감자탕",
           "순대", "수육", "마라탕", "샤브", "식사권", "외식", "한끼",
           "카페", "커피", "디저트", "베이커리", "케이크", "케익", "빵집", "제과", "마카롱", "브런치",
           "와플", "빙수", "쿠키", "도넛", "아이스크림", "젤라또", "휘낭시에", "까눌레", "스무디",
           "베이글", "크로플", "약과", "다과", "에이드", "라떼",
           "배달앱", "배달의민족", "배민", "요기요", "쿠팡이츠", "배달체험"],
    # 배송 = 택배로 받는 제품
    "배송": ["배송", "택배", "제품", "식품", "집으로", "생활용품", "가전", "의류", "패션",
           "반려", "강아지", "고양이", "애견", "펫", "꽃다발", "플라워"],
}


# 표준 카테고리 집합(8종). 어댑터가 준 값이 이 안에 있으면 그대로 신뢰, 아니면 classify.
CANONICAL = set(CATEGORY_KEYWORDS) | {"기타"}


def classify(text: str) -> str:
    """텍스트를 표준 카테고리 하나로 분류. 어떤 키워드에도 안 걸리면 '기타'.
    (사전 순서가 우선순위 - 포장·배달·페이백·기자단·숙박·카페·뷰티·건강·스포츠·문화·체험·맛집·생활·편의·배송)"""
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


def classify_with_fallback(text: str, fallback: str = "") -> str:
    """내용(키워드)으로 먼저 분류. 못 잡으면(기타) 어댑터가 준 유형 fallback(배송/기자단 등)을 쓰고,
    그것도 표준이 아니면 '기타'. → 유형이 내용을 덮어쓰지 않게(예: 피부과를 '배송'으로 만들지 않게)."""
    c = classify(text)
    if c != "기타":
        return c
    return fallback if fallback in CANONICAL else "기타"


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
