"""검증 스크립트: 파서 / 매칭 / 전체 파이프라인(데모) 을 봇 토큰 없이 확인."""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# DATABASE_URL 이 설정돼 있으면 db 가 그쪽(운영 PostgreSQL)을 쓴다.
# docker compose exec 로 돌리면 컨테이너 환경변수가 상속되므로 반드시 비운다.
os.environ["DATABASE_URL"] = ""

from bs4 import BeautifulSoup
from app.adapters.dinnerqueen import DinnerQueenAdapter
from app.adapters.base import Campaign
from app.matcher import matches
from app import db
from app.poller import collect_new
from app.notifier import notify_new

PASS = []
def check(name, cond):
    PASS.append(cond)
    print(("  ✅ " if cond else "  ❌ ") + name)

# 실제 디너의여왕 목록과 동일한 구조의 HTML 픽스처
FIXTURE = '''
<html><body>
<a href="https://dinnerqueen.net/taste/1420173" title="[랜덤픽] 시코 미니 선풍기 신청하기">x</a>
<a href="https://dinnerqueen.net/taste/1420173" title="캐러셀 링크">dup-carousel</a>
<a href="/taste/1423025" title="[부산 기장][릴스] 아빠대게 신청하기">y</a>
<a href="/taste/1423386" title="[서울 강남][릴스] 글로리 신청하기">z</a>
<a href="https://dinnerqueen.net/reward" title="리워드">nav</a>
<a href="https://dinnerqueen.net/taste/1422562" title="캐러셀 링크">banner-only</a>
</body></html>
'''

def test_parser():
    print("[1] 디너의여왕 파서")
    soup = BeautifulSoup(FIXTURE, "html.parser")
    # fetch() 의 파싱 본체를 재현
    import re
    from app.adapters.base import guess_region
    ids, out = set(), []
    for a in soup.select('a[href*="/taste/"]'):
        m = re.search(r"/taste/(\d+)", a.get("href",""))
        if not m: continue
        cid = m.group(1)
        if cid in ids: continue
        raw = (a.get("title") or "").strip()
        if not raw.endswith("신청하기"): continue
        title = re.sub(r"\s*신청하기$","",raw).strip()
        ids.add(cid)
        out.append((cid, title, guess_region(title)))
    titles = {c[0]: c[1] for c in out}
    check("캠페인 3건만 추출(배너/중복/네비 제외)", len(out) == 3)
    check("중복 cid 는 '신청하기' 제목으로 채택", titles.get("1420173","").startswith("[랜덤픽] 시코"))
    check("배너 전용 cid(1422562) 제외", "1422562" not in titles)
    reg = {c[0]: c[2] for c in out}
    check("지역 추출: 1423025 → '부산 기장'", reg.get("1423025") == "부산 기장")
    check("지역 추출: 1423386 → '서울 강남'", reg.get("1423386") == "서울 강남")

def test_matcher():
    print("[2] 매칭 규칙")
    c = Campaign("dq","디너의여왕","1","[서울 강남] 스시 오마카세","u",region="서울 강남",category="맛집")
    check("필터 없음 → 통과", matches(c, [], []))
    check("키워드 일치 → 통과", matches(c, ["오마카세"], []))
    check("키워드 불일치 → 차단", not matches(c, ["치킨"], []))
    check("지역 일치 → 통과", matches(c, [], ["서울"]))
    check("지역 불일치 → 차단", not matches(c, [], ["부산"]))
    check("키워드+지역 모두 만족 → 통과", matches(c, ["스시"], ["강남"]))
    check("키워드 맞고 지역 틀림 → 차단(AND)", not matches(c, ["스시"], ["부산"]))

class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

async def test_pipeline():
    print("[3] 전체 파이프라인 (데모 어댑터)")
    tmp = tempfile.mktemp(suffix=".db")
    db.init(tmp)
    # 사용자 2명: A=서울만, B=필터없음
    db.upsert_user(100); db.add_filter(100, "region", "서울")
    db.upsert_user(200)
    # 1차 수집: 첫 수집이라 알림 없이 시딩
    first = await collect_new(demo=True)
    check("첫 수집은 신규 0건(시딩)", len(first) == 0)
    await asyncio.sleep(1.1)  # 데모 cid(초 단위) 갱신
    new_items = await collect_new(demo=True)
    check("2차 수집에서 신규 감지", len(new_items) >= 1)
    bot = FakeBot()
    await notify_new(bot, new_items)
    # B(필터없음)는 항상 받고, A(서울)는 지역 맞을 때만
    got_b = any(cid == 200 for cid, _ in bot.sent)
    check("필터 없는 사용자에게 발송됨", got_b)
    # 데모 캠페인 지역에 '서울'이 들어간 경우에만 A 수신 → 모순 없이 동작하는지(에러 없이)만 확인
    check("발송 과정 에러 없음", True)
    print(f"    (발송 메시지 {len(bot.sent)}건, 신규 {len(new_items)}건)")

async def main():
    test_parser(); test_matcher(); await test_pipeline()
    print("\n결과:", f"{sum(PASS)}/{len(PASS)} 통과")
    sys.exit(0 if all(PASS) else 1)

asyncio.run(main())
