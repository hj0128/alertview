"""캠페인 피드 + NEW(개별/모두 확인) 검증."""
import os, tempfile, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update({"TELEGRAM_BOT_TOKEN":"1:A","TELEGRAM_BOT_USERNAME":"b","WEB_SECRET":"s",
  "DB_PATH":tempfile.mktemp(suffix=".db"),"WEB_DEV_LOGIN":"1","WEB_DEV_CHATID":"42"})
from app import db, config; db.init(config.DB_PATH)
from app.adapters.base import Campaign
from app import web
from fastapi.testclient import TestClient

P=[]; ck=lambda n,x:(P.append(bool(x)),print(("  ✅ " if x else "  ❌ ")+n))
db.upsert_user(42); time.sleep(1.05)
db.record_campaign(Campaign("dq","d","100","[서울 강남][블로그] 오마카세","u1",region="서울 강남",category="맛집",channel="블로그",dday=2,applicants=2,recruit=8,competition=0.2))
db.record_campaign(Campaign("dq","d","101","[부산][릴스] 카페","u2",region="부산",category="카페",channel="릴스",dday=9,applicants=30,recruit=2,competition=15.0))
db.record_campaign(Campaign("dq","d","102","[강원 횡성][블로그] 한우","u3",region="강원 횡성",category="맛집",channel="블로그",dday=1,applicants=1,recruit=5,competition=0.2))
c=TestClient(web.app); c.get("/dev-login")
d=c.get("/api/campaigns").json(); ck("전체 3건",d["total"]==3); ck("전부 NEW",d["new_count"]==3)
c.post("/api/category/toggle",json={"value":"맛집"})
ck("맛집 필터 2건",c.get("/api/campaigns").json()["total"]==2)
c.post("/api/scalar",json={"key":"max_competition","value":1})
ck("경쟁률≤1 적용 2건",c.get("/api/campaigns").json()["total"]==2)
c.post("/api/view",json={"site":"dq","cid":"100"})
d=c.get("/api/campaigns").json(); ck("개별 확인 NEW 감소",d["new_count"]==1)
c.post("/api/seen-all"); ck("모두 확인 NEW=0",c.get("/api/campaigns").json()["new_count"]==0)
print("\n결과:", f"{sum(P)}/{len(P)} 통과"); sys.exit(0 if all(P) else 1)
