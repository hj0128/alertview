"""웹 UI 검증: 로그인 해시 검증 + 필터 API + 개발 로그인 (봇/도메인 없이)."""
import os, sys, tempfile, hashlib, hmac, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DUMMY = "123456:TESTTOKENabcdef"
os.environ.update({
    "TELEGRAM_BOT_TOKEN": DUMMY, "TELEGRAM_BOT_USERNAME": "test_bot",
    "WEB_SECRET": "test-secret", "DB_PATH": tempfile.mktemp(suffix=".db"),
    "WEB_DEV_LOGIN": "1", "WEB_DEV_CHATID": "777",
})
from app import db, config
db.init(config.DB_PATH)
from app import web
from fastapi.testclient import TestClient

PASS=[]
def check(n,c): PASS.append(c); print(("  ✅ " if c else "  ❌ ")+n)

def signed(p):
    cs="\n".join(f"{k}={p[k]}" for k in sorted(p) if k!="hash")
    p["hash"]=hmac.new(hashlib.sha256(DUMMY.encode()).digest(),cs.encode(),hashlib.sha256).hexdigest()
    return p

print("[1] 로그인 해시 검증")
good=signed({"id":"555","first_name":"현주","auth_date":str(int(time.time()))})
check("올바른 해시 통과", web.verify_telegram_auth(dict(good)))
bad=dict(good); bad["hash"]="deadbeef"
check("위조 해시 거부", not web.verify_telegram_auth(bad))

print("[2] API 흐름 (텔레그램 로그인)")
c=TestClient(web.app)
check("미로그인 401", c.get("/api/state").status_code==401)
check("로그인 303", c.get("/auth", params=good, follow_redirects=False).status_code==303)
c.post("/api/keyword/add", json={"value":"횡성"})
c.post("/api/region/toggle", json={"value":"강원"})
st=c.get("/api/state").json()
check("키워드 저장", st["keywords"]==["횡성"])
check("지역 저장", "강원" in st["regions"])
c.post("/api/region/toggle", json={"value":"강원"})
check("지역 토글 삭제", "강원" not in c.get("/api/state").json()["regions"])
check("DB 동기화 확인", db.get_filters(555)[0]==["횡성"])

print("[3] 개발 로그인 (로컬 테스트)")
c2=TestClient(web.app)
check("dev-login 303", c2.get("/dev-login", follow_redirects=False).status_code==303)
st2=c2.get("/api/state").json()
check("dev 기본 chatid=777로 로그인", db.is_active(777) and st2["active"]==True)
c2.post("/api/keyword/add", json={"value":"오마카세"})
check("dev 세션도 필터 저장됨", db.get_filters(777)[0]==["오마카세"])
check("로그인 페이지에 개발버튼 노출", "/dev-login" in web._login_html())

print("\n결과:", f"{sum(PASS)}/{len(PASS)} 통과")
sys.exit(0 if all(PASS) else 1)
