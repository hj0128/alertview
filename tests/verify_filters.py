"""신규 필터 검증: 디너의여왕 카드 파싱 + 카테고리/채널/경쟁률/마감일 매칭."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TELEGRAM_BOT_TOKEN"]="x:y"; os.environ["DB_PATH"]=tempfile.mktemp(suffix=".db")

from bs4 import BeautifulSoup
from app.adapters.dinnerqueen import _card_node, _APPLY_RE, _DDAY_RE
from app.adapters.base import Campaign, guess_in, guess_region, CHANNELS, CATEGORIES
from app.matcher import matches

PASS=[]
def check(n,c): PASS.append(bool(c)); print(("  ✅ " if c else "  ❌ ")+n)

# 디너의여왕 카드 구조를 모사한 HTML (카드=div, 내부에 D-day/카테고리/채널/신청·모집)
FIX='''<div class="list">
  <div class="card">
     <a href="/taste/1423025" title="[부산 기장][릴스] 아빠대게 신청하기">img</a>
     <span>D-6</span><span>맛집</span><span>릴스</span>
     <span>[부산 기장][릴스] 아빠대게</span>
     <span>신청 19 / 모집 1</span>
  </div>
  <div class="card">
     <a href="/taste/1423322" title="[대전 유성][블로그] 탑립커피 신청하기">img</a>
     <span>D-2</span><span>카페</span><span>블로그</span>
     <span>신청 2 / 모집 8</span>
  </div>
</div>'''

print("[1] 카드 파싱")
soup=BeautifulSoup(FIX,"html.parser")
a1=soup.select_one('a[href="/taste/1423025"]')
t1=_card_node(a1).get_text(" ", strip=True)
check("카드 텍스트 격리(신청/모집 1건)", len(_APPLY_RE.findall(t1))==1)
am=_APPLY_RE.search(t1); check("신청 19 / 모집 1 추출", am.group(1)=="19" and am.group(2)=="1")
check("D-day 6 추출", _DDAY_RE.search(t1).group(1)=="6")
check("채널 릴스 인식", guess_in(t1,CHANNELS)=="릴스")
check("카테고리 맛집 인식", guess_in(t1,CATEGORIES)=="맛집")
check("지역 부산 기장 인식", guess_region("[부산 기장][릴스] 아빠대게")=="부산 기장")

print("[2] 새 필터 매칭")
c=Campaign("dq","디너의여왕","1","[강원 횡성][블로그] 한우집","u",
           region="강원 횡성",category="맛집",channel="블로그",
           dday=2,applicants=2,recruit=8,competition=0.2)
check("카테고리 일치 통과", matches(c, categories=["맛집"]))
check("카테고리 불일치 차단", not matches(c, categories=["카페"]))
check("채널 일치 통과", matches(c, channels=["블로그"]))
check("채널 불일치 차단", not matches(c, channels=["릴스"]))
check("경쟁률 ≤1 통과(0.2)", matches(c, max_competition=1))
check("마감 ≤3 통과(D-2)", matches(c, max_dday=3))
high=Campaign("dq","d","2","x","u",competition=6.0,dday=10)
check("경쟁률 6 > 1 차단", not matches(high, max_competition=1))
check("마감 D-10 > 3 차단", not matches(high, max_dday=3))
unknown=Campaign("dq","d","3","x","u")  # competition/dday None
check("경쟁률 미상이면 제외 안함", matches(unknown, max_competition=1))
check("복합: 맛집+경쟁률≤1 모두 만족", matches(c, categories=["맛집"], max_competition=1))
check("복합: 맛집+채널릴스(불일치) 차단", not matches(c, categories=["맛집"], channels=["릴스"]))

print("\n결과:", f"{sum(PASS)}/{len(PASS)} 통과")
sys.exit(0 if all(PASS) else 1)
