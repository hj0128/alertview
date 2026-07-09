"""웹 UI: 텔레그램 로그인 + 키워드/지역 설정. 봇과 같은 SQLite DB를 공유."""
from __future__ import annotations
import datetime
import hashlib
import hmac
import json
import re
import time
from html import escape as _esc

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import db, config
from .matcher import matches_filter, category_of
from .adapters import ALL_ADAPTERS
from .adapters.base import Campaign

# 사용자에게 노출할 체험단(사이트) 목록 = 활성 어댑터
WEB_SITES = [{"key": a.key, "name": a.name} for a in ALL_ADAPTERS if a.enabled]
_SITE_NAMES = {a.key: a.name for a in ALL_ADAPTERS}

WEB_REGIONS = [
    "서울", "경기", "인천", "강원", "충북", "충남", "대전", "세종",
    "전북", "전남", "광주", "경북", "경남", "대구", "울산", "부산", "제주",
]
WEB_CATEGORIES = ["맛집", "여가", "뷰티", "배송", "포장", "페이백", "기자단", "기타"]
WEB_CHANNELS = ["블로그", "클립", "인스타", "릴스", "유튜브", "숏츠", "틱톡", "기타"]

# 숫자 범위 필터(각각 min~max). 저장(스칼라)·프리셋·전체해제·상태응답이 같은 목록을 공유한다.
NUMERIC_FILTERS = ("min_competition", "max_competition", "min_dday", "max_dday",
                   "min_recruit", "max_recruit", "min_applicants", "max_applicants")

app = FastAPI(title="체험단 알림 설정")
app.add_middleware(SessionMiddleware, secret_key=config.WEB_SECRET, max_age=60 * 60 * 24 * 30)


@app.middleware("http")
async def _visit_logger(request: Request, call_next):
    """페이지(HTML) 방문을 서버에 기록. API/정적/인증콜백은 제외. 베스트에포트."""
    response = await call_next(request)
    try:
        p = request.url.path
        if (request.method == "GET" and response.status_code < 400
                and not p.startswith(("/api", "/static", "/auth", "/dev-login", "/logout"))
                and p not in ("/favicon.ico", "/robots.txt")):
            xff = request.headers.get("x-forwarded-for", "")
            ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")
            try:
                uid = request.session.get("uid")
            except Exception:
                uid = None
            db.log_visit(ip, p, request.headers.get("referer", ""),
                         request.headers.get("user-agent", ""), uid)
    except Exception:
        pass
    return response


@app.get("/admin/stats", response_class=HTMLResponse)
async def admin_stats(request: Request):
    """관리자 전용 방문 통계 (세션 uid == ADMIN_CHAT_ID)."""
    uid = request.session.get("uid")
    if not config.ADMIN_CHAT_ID or str(uid) != str(config.ADMIN_CHAT_ID):
        return HTMLResponse("<h3>권한 없음</h3><p>관리자 계정으로 로그인 후 접근하세요.</p>", status_code=403)
    s = db.visit_stats(14)
    daily = "".join(f"<tr><td>{d['d']}</td><td>{d['v']}</td><td>{d['u']}</td></tr>" for d in s["daily"]) or "<tr><td colspan=3>기록 없음</td></tr>"
    refs = "".join(f"<tr><td>{_esc(str(r['r']))[:80]}</td><td>{r['n']}</td></tr>" for r in s["referrers"]) or "<tr><td colspan=2>-</td></tr>"
    paths = "".join(f"<tr><td>{_esc(str(p['path']))[:80]}</td><td>{p['n']}</td></tr>" for p in s["paths"]) or "<tr><td colspan=2>-</td></tr>"
    t, tot = s["today"], s["total"]
    html_out = f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>방문 통계</title>
<style>body{{font-family:-apple-system,'Malgun Gothic',sans-serif;background:#f5f6f8;margin:0;padding:20px;color:#1f2533}}
h2{{margin:18px 0 8px}} .cards{{display:flex;gap:12px;flex-wrap:wrap}}
.card{{background:#fff;border-radius:12px;padding:16px 20px;box-shadow:0 1px 6px rgba(0,0,0,.06)}}
.big{{font-size:28px;font-weight:700;color:#3b6ef6}} .lbl{{color:#888;font-size:13px}}
table{{border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 6px rgba(0,0,0,.06);min-width:280px}}
th,td{{padding:8px 14px;text-align:left;border-bottom:1px solid #eef}} th{{background:#f0f3fa}}</style></head><body>
<h1>📊 방문 통계 <span style="font-size:14px;color:#888">(최근 {s['days']}일)</span></h1>
<div class=cards>
 <div class=card><div class=lbl>오늘 방문</div><div class=big>{t['v']}</div><div class=lbl>순 방문자 {t['u']}</div></div>
 <div class=card><div class=lbl>{s['days']}일 방문</div><div class=big>{tot['v']}</div><div class=lbl>순 방문자 {tot['u']}</div></div>
</div>
<h2>일별</h2><table><tr><th>날짜</th><th>방문</th><th>순방문(IP)</th></tr>{daily}</table>
<h2>유입 경로</h2><table><tr><th>referrer</th><th>수</th></tr>{refs}</table>
<h2>페이지</h2><table><tr><th>경로</th><th>수</th></tr>{paths}</table>
</body></html>"""
    return HTMLResponse(html_out, headers={"Cache-Control": "no-store"})


def _is_admin(request: Request) -> bool:
    return bool(config.ADMIN_CHAT_ID) and str(request.session.get("uid")) == str(config.ADMIN_CHAT_ID)


@app.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    if not _is_admin(request):
        return HTMLResponse("<h3>권한 없음</h3><p>관리자 계정으로 로그인 후 접근하세요.</p>", status_code=403)
    health = db.get_adapter_health()
    inqs = db.list_inquiries(50)
    vs = db.visit_stats(7)
    _col = {"ok": "#15a34a", "zero": "#e11d48", "error": "#e11d48", "partial": "#e59409"}

    def bad_first(h):
        return (0 if h["status"] in ("error", "zero") else 1 if h["status"] == "partial" else 2, h["key"])
    hrows = "".join(
        f"<tr><td>{_esc(_SITE_NAMES.get(h['key'], h['key']))}</td>"
        f"<td><b style=\"color:{_col.get(h['status'], '#888')}\">{h['status']}</b></td>"
        f"<td>{h['cnt']}</td><td>{(h['ts'] or '')[:16]}</td>"
        f"<td>{_esc(str(h.get('note') or ''))[:70]}</td></tr>"
        for h in sorted(health, key=bad_first)) or "<tr><td colspan=5>수집 기록 없음(폴러 1회 실행 후 표시)</td></tr>"
    irows = "".join(
        f"<tr><td>{(i['ts'] or '')[:16]}</td><td>{_esc(str(i.get('contact') or '-'))}</td>"
        f"<td>{_esc(str(i['message']))}</td></tr>" for i in inqs) or "<tr><td colspan=3>문의 없음</td></tr>"
    html_out = f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>관리자</title>
<style>body{{font-family:-apple-system,'Malgun Gothic',sans-serif;background:#f5f6f8;margin:0;padding:20px;color:#1f2533}}
h2{{margin:22px 0 8px}} a{{color:#3b6ef6}}
table{{border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 6px rgba(0,0,0,.06);width:100%;max-width:820px}}
th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid #eef;font-size:14px;vertical-align:top}} th{{background:#f0f3fa}}</style></head><body>
<h1>🛠 관리자 대시보드</h1>
<p>최근 7일 방문 <b>{vs['total']['v']}</b> (순 {vs['total']['u']}) · <a href="/admin/stats">방문 통계 자세히 →</a></p>
<h2>📡 수집 상태 (사이트별)</h2>
<table><tr><th>사이트</th><th>상태</th><th>건수</th><th>마지막 수집</th><th>비고</th></tr>{hrows}</table>
<h2>💬 사용자 문의 ({db.count_new_inquiries()} 신규)</h2>
<table><tr><th>시각</th><th>연락처</th><th>내용</th></tr>{irows}</table>
</body></html>"""
    return HTMLResponse(html_out, headers={"Cache-Control": "no-store"})


@app.get("/inquiry", response_class=HTMLResponse)
async def inquiry_page(request: Request):
    return HTMLResponse(f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>문의/건의</title>
<style>body{{font-family:-apple-system,'Malgun Gothic',sans-serif;background:#f5f6f8;margin:0;padding:24px;color:#1f2533}}
.card{{background:#fff;border-radius:16px;padding:24px;max-width:480px;margin:0 auto;box-shadow:0 2px 16px rgba(0,0,0,.06)}}
h1{{font-size:20px}} label{{display:block;margin:14px 0 6px;font-weight:600}}
input,textarea{{width:100%;box-sizing:border-box;padding:10px;border:1px solid #dde;border-radius:8px;font-size:15px}}
textarea{{min-height:120px}} button{{margin-top:16px;width:100%;padding:12px;background:#3b6ef6;color:#fff;border:0;border-radius:8px;font-size:16px;cursor:pointer}}
.msg{{margin-top:12px;color:#15a34a}}</style></head><body><div class=card>
<h1>💬 문의 / 체험단 추가 요청</h1>
<p style="color:#666;font-size:14px">넣었으면 하는 체험단, 버그, 건의 등 자유롭게 남겨주세요.</p>
<label>연락처 (선택 — 이메일/텔레그램 등, 답변 원하시면)</label><input id=contact maxlength=120>
<label>내용</label><textarea id=message maxlength=2000 placeholder="예: OO체험단도 추가해주세요"></textarea>
<button onclick="send()">보내기</button><div class=msg id=msg></div></div>
<script>
async function send(){{
 var m=document.getElementById('message').value.trim();
 if(!m){{document.getElementById('msg').style.color='#e11';document.getElementById('msg').textContent='내용을 입력해주세요.';return;}}
 var r=await fetch('/api/inquiry',{{method:'POST',headers:{{'Content-Type':'application/json'}},
   body:JSON.stringify({{contact:document.getElementById('contact').value,message:m}})}});
 if(r.ok){{document.getElementById('msg').style.color='#15a34a';document.getElementById('msg').textContent='접수됐어요. 감사합니다! 🙏';
   document.getElementById('message').value='';document.getElementById('contact').value='';}}
 else{{document.getElementById('msg').style.color='#e11';document.getElementById('msg').textContent='잠시 후 다시 시도해주세요.';}}
}}
</script></body></html>""", headers={"Cache-Control": "no-store"})


@app.post("/api/inquiry")
async def api_inquiry(request: Request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    msg = (b.get("message") or "").strip()
    if not msg:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    db.add_inquiry(request.session.get("uid"), (b.get("contact") or "").strip(), msg)
    return JSONResponse({"ok": True})


@app.get("/api/map")
async def api_map(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    today = datetime.date.today()
    out = []
    for p in db.active_place_points():
        dl = p.get("deadline")
        dday = None
        if dl:
            try:
                d = (datetime.date.fromisoformat(dl) - today).days
                dday = d if d >= 0 else None
            except ValueError:
                pass
        out.append({
            "lat": p["lat"], "lng": p["lng"], "title": p["title"] or "",
            "url": p["url"] or "", "region": p["region"] or "",
            "category": category_of(p["category"], p["title"] or ""),
            "place": p["place"] or "", "dday": dday,
            "site": _SITE_NAMES.get(p["site"], p["site"]),
        })
    return {"points": out}


@app.get("/map", response_class=HTMLResponse)
async def map_page(request: Request):
    return HTMLResponse("""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>지역 지도 · 체험단</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<style>html,body{margin:0;height:100%;font-family:-apple-system,'Malgun Gothic',sans-serif}
#bar{position:fixed;z-index:1000;top:0;left:0;right:0;height:46px;background:#fff;box-shadow:0 1px 6px rgba(0,0,0,.1);
  display:flex;align-items:center;gap:12px;padding:0 14px}
#bar b{font-size:15px} #bar a{color:#3b6ef6;text-decoration:none;font-size:14px;margin-left:auto}
#map{position:absolute;top:46px;bottom:0;left:0;right:0;background:#eaeaea}
#leg{font-size:12px;color:#555;display:flex;gap:9px;align-items:center}
#leg i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:3px;vertical-align:middle;border:1px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.15)}
.cmark .b{color:#fff;border-radius:50%;min-width:30px;height:30px;padding:0 6px;
  display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;
  box-shadow:0 2px 8px rgba(0,0,0,.35);border:3px solid #fff}
.cmark .b.clus{background:#f06418}</style></head><body>
<div id=bar><b>🗺 체험단 지도</b><span style="color:#888;font-size:13px" id=hint>불러오는 중…</span>
<span id=leg><span><i style="background:#e4572e"></i>맛집</span><span><i style="background:#2e9e5b"></i>여가</span><span><i style="background:#d6336c"></i>뷰티</span></span>
<a href="/">← 피드로</a></div>
<div id=map></div>
<script>
const map=L.map('map',{scrollWheelZoom:true}).setView([36.4,127.9],7);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
  {maxZoom:19,subdomains:'abcd',attribution:'© OpenStreetMap © CARTO'}).addTo(map);
const CATC={'맛집':'#e4572e','여가':'#2e9e5b','뷰티':'#d6336c','배송':'#7048e8','포장':'#f08c00','페이백':'#1c7ed6','기자단':'#495057','기타':'#868e96'};
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function pin(cat){const col=CATC[cat]||'#3b6ef6';   // 물방울 핀(끝점이 위치)
  return L.divIcon({className:'cmark',iconSize:[26,34],iconAnchor:[13,33],popupAnchor:[0,-30],
    html:'<svg width=26 height=34 viewBox="0 0 26 34" xmlns="http://www.w3.org/2000/svg">'
      +'<path d="M13 1C6.4 1 1 6.4 1 13c0 8.5 12 20 12 20s12-11.5 12-20C25 6.4 19.6 1 13 1z" fill="'+col+'" stroke="#fff" stroke-width="2"/>'
      +'<circle cx=13 cy=13 r=4.4 fill="#fff"/></svg>'});}
function bubble(n){const sz=Math.min(56,24+Math.round(Math.log2(n+1))*3.5);
  return L.divIcon({className:'cmark',html:'<div class="b clus" style="min-width:'+sz+'px;height:'+sz+'px">'+n+'</div>',iconSize:[sz,sz]});}
const cluster=L.markerClusterGroup({
  showCoverageOnHover:false, spiderfyOnMaxZoom:true, maxClusterRadius:44,
  iconCreateFunction:c=>bubble(c.getChildCount())      // 클러스터 = 그 안의 캠페인 수
});
fetch('/api/map').then(r=>r.json()).then(d=>{
  const pts=d.points||[];
  document.getElementById('hint').textContent=pts.length.toLocaleString()+'개 캠페인 (실제 위치)';
  pts.forEach(p=>{
    if(p.lat==null||p.lng==null)return;
    const dd=(p.dday==null)?'':(p.dday===0?'오늘마감':'D-'+p.dday);
    const html='<b>'+esc(p.title)+'</b><br>'
      +'<span style="color:#666">'+(p.place?esc(p.place)+' · ':'')+esc(p.region)+'</span>'
      +(dd?' · <b style="color:#e4572e">'+dd+'</b>':'')+'<br>'
      +'<span style="color:#888;font-size:12px">'+esc(p.category)+' · '+esc(p.site)+'</span><br>'
      +'<a href="'+esc(p.url)+'" target="_blank" rel="noopener">캠페인 보러가기 →</a>';
    const m=L.marker([p.lat,p.lng],{icon:pin(p.category)});
    m.bindPopup(html);
    cluster.addLayer(m);
  });
  map.addLayer(cluster);
}).catch(e=>{document.getElementById('hint').textContent='불러오기 실패';});
</script></body></html>""", headers={"Cache-Control": "no-store"})


def verify_telegram_auth(data: dict) -> bool:
    recv = data.get("hash")
    if not recv or not config.BOT_TOKEN:
        return False
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data) if k != "hash")
    secret = hashlib.sha256(config.BOT_TOKEN.encode()).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv):
        return False
    try:
        if time.time() - int(data.get("auth_date", "0")) > 86400:
            return False
    except ValueError:
        return False
    return True


def _uid(request: Request):
    return request.session.get("uid")


_MERGE_BRK = re.compile(r"\[[^\]]*\]")           # [지역]·[채널]·[카테고리] 태그
_MERGE_NONCORE = re.compile(r"[\s\-_/()·.,!~+]+")


def _merge_key(title: str, region: str) -> str:
    """중복 병합 키: 대괄호 태그 제거 + 공백/기호 제거한 제목 + 지역. 완전일치만 병합(오병합 방지)."""
    t = _MERGE_NONCORE.sub("", _MERGE_BRK.sub("", title or "")).lower()
    return (region or "").replace(" ", "") + "|" + t


def _collapse(items: list) -> list:
    """정렬된 목록에서 같은 키를 대표 1건으로 접고, 나머지는 dup_count/dup_sites 로 표기."""
    keys, out = {}, []
    for d in items:
        k = _merge_key(d["title"], d["region"])
        rep = keys.get(k)
        if rep is not None:
            rep["dup_count"] += 1
            if d["site_name"] not in rep["dup_sites"]:
                rep["dup_sites"].append(d["site_name"])
        else:
            d["dup_count"] = 1
            d["dup_sites"] = [d["site_name"]]
            keys[k] = d
            out.append(d)
    return out


@app.get("/dev-login")
async def dev_login(request: Request):
    """로컬 테스트 전용: 텔레그램 로그인 없이 세션 생성. WEB_DEV_LOGIN=1 일 때만."""
    if not config.WEB_DEV_LOGIN:
        return HTMLResponse("dev login disabled", status_code=404)
    raw = request.query_params.get("id") or config.WEB_DEV_CHATID or "999999999"
    try:
        chat_id = int(raw)
    except ValueError:
        return HTMLResponse("id 는 숫자여야 합니다", status_code=400)
    db.upsert_user(chat_id)
    request.session["uid"] = chat_id
    request.session["name"] = "(개발 로그인)"
    return RedirectResponse("/", status_code=303)


@app.get("/auth")
async def auth(request: Request):
    params = dict(request.query_params)
    if not verify_telegram_auth(params):
        return HTMLResponse("<h3>로그인 검증 실패</h3><a href='/'>돌아가기</a>", status_code=401)
    chat_id = int(params["id"])
    db.upsert_user(chat_id)
    request.session["uid"] = chat_id
    request.session["name"] = params.get("first_name", "")
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


def _region_tree_ordered() -> dict:
    """db.region_tree()를 시/도 알려진 순서(WEB_REGIONS)대로 정렬해 반환.
    '전국'은 노출 안 함(미선택=전체이므로 불필요), '기타'는 항상 맨 끝."""
    tree = db.region_tree()
    tree.pop("전국", None)
    order = {name: i for i, name in enumerate(WEB_REGIONS)}

    def rank(s):
        if s == "기타":
            return len(order) + 1
        return order.get(s, len(order))
    keys = sorted(tree.keys(), key=lambda s: (rank(s), s))
    return {k: tree[k] for k in keys}


@app.get("/api/state")
async def state(request: Request):
    uid = _uid(request)
    region_tree = _region_tree_ordered()
    if not uid:
        # 비로그인(게스트): 설정값은 비우고 logged_in=False 로 응답.
        return {
            "logged_in": False,
            "active": False,
            "name": "",
            "sites": [],
            "keywords": [], "regions": [], "categories": [], "channels": [],
            **{k: None for k in NUMERIC_FILTERS},
            "all_sites": WEB_SITES,
            "all_regions": WEB_REGIONS,
            "all_categories": WEB_CATEGORIES,
            "all_channels": WEB_CHANNELS,
            "region_tree": region_tree,
        }
    f = db.get_all_filters(uid)
    presets = []
    for p in db.list_presets(uid):
        try:
            presets.append({"name": p["name"], "payload": json.loads(p["payload"] or "{}")})
        except (ValueError, TypeError):
            pass
    return {
        "logged_in": True,
        "active": db.is_active(uid),
        "name": request.session.get("name", ""),
        "presets": presets,
        "sites": f["sites"],
        "keywords": f["keywords"],
        "regions": f["regions"],
        "categories": f["categories"],
        "channels": f["channels"],
        **{k: f[k] for k in NUMERIC_FILTERS},
        "all_sites": WEB_SITES,
        "all_regions": WEB_REGIONS,
        "all_categories": WEB_CATEGORIES,
        "all_channels": WEB_CHANNELS,
        "region_tree": region_tree,
    }


@app.post("/api/keyword/add")
async def kw_add(request: Request):
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    val = (body.get("value") or "").strip()
    if val:
        db.add_filter(uid, "keyword", val)
    return {"ok": True}


@app.post("/api/keyword/del")
async def kw_del(request: Request):
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    db.remove_filter(uid, "keyword", (body.get("value") or "").strip())
    return {"ok": True}


@app.post("/api/site/toggle")
async def site_toggle(request: Request):
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    val = ((await request.json()).get("value") or "").strip()
    cur = db.list_values(uid, "site")
    db.remove_filter(uid, "site", val) if val in cur else db.add_filter(uid, "site", val)
    return {"ok": True}


@app.post("/api/region/toggle")
async def region_toggle(request: Request):
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    val = (body.get("value") or "").strip()
    _, regs = db.get_filters(uid)
    if val in regs:
        db.remove_filter(uid, "region", val)
    else:
        db.add_filter(uid, "region", val)
    return {"ok": True}


@app.post("/api/region/set")
async def region_set(request: Request):
    """지역 필터 전체를 한 번에 교체(시/도 '전체' ↔ 구 자동 정리용)."""
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    vals = body.get("values") or []
    vals = [str(v).strip() for v in vals if str(v).strip()]
    db.clear_filters(uid, "region")
    for v in vals:
        db.add_filter(uid, "region", v)
    return {"ok": True}


@app.post("/api/category/toggle")
async def category_toggle(request: Request):
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    val = ((await request.json()).get("value") or "").strip()
    cur = db.list_values(uid, "category")
    db.remove_filter(uid, "category", val) if val in cur else db.add_filter(uid, "category", val)
    return {"ok": True}


@app.post("/api/channel/toggle")
async def channel_toggle(request: Request):
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    val = ((await request.json()).get("value") or "").strip()
    cur = db.list_values(uid, "channel")
    db.remove_filter(uid, "channel", val) if val in cur else db.add_filter(uid, "channel", val)
    return {"ok": True}


@app.post("/api/clear")
async def clear(request: Request):
    """필터 전체 해제. type='all' 이면 모든 조건, 아니면 해당 종류만."""
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    t = ((await request.json()).get("type") or "").strip()
    if t == "all":
        db.clear_filters(uid)                 # 모든 ftype(키워드·지역·사이트·카테고리·채널·스칼라) 삭제
    elif t in ("site", "keyword", "region", "category", "channel") + NUMERIC_FILTERS:
        db.clear_filters(uid, t)
    else:
        return JSONResponse({"error": "bad type"}, status_code=400)
    return {"ok": True}


# 저장된 검색조건(프리셋): payload 키 ↔ filters 테이블 ftype
_PRESET_KEYS = ("sites", "keywords", "regions", "categories", "channels")
_PRESET_FTYPE = {"sites": "site", "keywords": "keyword", "regions": "region",
                 "categories": "category", "channels": "channel"}


def _normalize_payload(p: dict) -> dict:
    out = {}
    for k in _PRESET_KEYS:
        vals = p.get(k) or []
        out[k] = [str(x).strip() for x in vals if str(x).strip()][:50]
    for k in NUMERIC_FILTERS:
        v = p.get(k)
        out[k] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return out


@app.post("/api/preset/save")
async def preset_save(request: Request):
    """현재(또는 전달된) 검색조건을 이름 붙여 저장."""
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = await request.json()
    name = (b.get("name") or "").strip()[:40]
    if not name:
        return JSONResponse({"error": "no name"}, status_code=400)
    payload = _normalize_payload(b.get("payload") or {})
    db.save_preset(uid, name, json.dumps(payload, ensure_ascii=False))
    return {"ok": True}


@app.post("/api/preset/apply")
async def preset_apply(request: Request):
    """저장된 조건을 현재 필터로 적용(기존 필터는 교체)."""
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = ((await request.json()).get("name") or "").strip()
    raw = next((p["payload"] for p in db.list_presets(uid) if p["name"] == name), None)
    if raw is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = _normalize_payload(json.loads(raw or "{}"))
    db.clear_filters(uid)
    for k in _PRESET_KEYS:
        for v in payload[k]:
            db.add_filter(uid, _PRESET_FTYPE[k], v)
    for k in NUMERIC_FILTERS:
        if payload[k] is not None:
            db.set_scalar(uid, k, payload[k])
    return {"ok": True}


@app.post("/api/preset/delete")
async def preset_delete(request: Request):
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.delete_preset(uid, ((await request.json()).get("name") or "").strip())
    return {"ok": True}


@app.post("/api/scalar")
async def set_scalar(request: Request):
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    key = body.get("key")
    if key not in NUMERIC_FILTERS:
        return JSONResponse({"error": "bad key"}, status_code=400)
    val = body.get("value")
    db.set_scalar(uid, key, val if val not in ("", None) else None)
    return {"ok": True}


@app.post("/api/active")
async def set_active(request: Request):
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    db.set_active(uid, bool(body.get("active")))
    return {"ok": True}


@app.get("/api/campaigns")
async def campaigns(request: Request, response: Response):
    """필터에 맞는 최근 캠페인 전체(최신순) + NEW 여부.
    비로그인(게스트)이면 필터 없이 전체를 보여주고 NEW 표시는 하지 않는다."""
    # D-day 는 매 요청 시 마감일-오늘로 계산되므로, 응답을 캐시하면 옛 D-day 가 보인다 → no-store.
    response.headers["Cache-Control"] = "no-store"
    uid = _uid(request)
    guest = not uid
    if guest:
        # 게스트: 저장 없이 쿼리파라미터로 받은 조건으로 즉석 필터링.
        qp = request.query_params

        def _csv(k):
            return [s.strip() for s in qp.get(k, "").split(",") if s.strip()]

        def _num(k):
            v = qp.get(k, "")
            try:
                return float(v) if v not in ("", None) else None
            except ValueError:
                return None

        def _int(k):
            v = _num(k)
            return int(v) if v is not None else None
        f = {
            "sites": _csv("sites"),
            "keywords": _csv("kw"), "regions": _csv("region"),
            "categories": _csv("cat"), "channels": _csv("ch"),
            "min_competition": _num("mincomp"), "max_competition": _num("maxcomp"),
            "min_dday": _int("mindday"), "max_dday": _int("maxdday"),
            "min_recruit": _int("minrec"), "max_recruit": _int("maxrec"),
            "min_applicants": _int("minapp"), "max_applicants": _int("maxapp"),
        }
        viewed = set()
        fav_set = set()
    else:
        f = db.get_all_filters(uid)
        viewed = db.viewed_set(uid)
        fav_set = db.favorites_set(uid)

    # 페이지네이션 파라미터(무한스크롤)
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
    except ValueError:
        offset = 0
    try:
        limit = int(request.query_params.get("limit", config.FEED_PAGE))
    except ValueError:
        limit = config.FEED_PAGE
    limit = max(1, min(limit, 200))
    sort = request.query_params.get("sort", "recent")
    if sort not in ("recent", "remaining", "deadline", "competition"):
        sort = "recent"
    fav_only = request.query_params.get("fav") == "1" and not guest
    q = request.query_params.get("q", "").strip().lower()   # 검색어(제목·지역)
    merge = request.query_params.get("merge", "1") != "0"   # 중복 병합(기본 ON)

    def _q_ok(r):
        if not q:
            return True
        return q in ((r["title"] or "") + " " + (r["region"] or "")).lower()

    # 최근 48시간 이내 수집 = NEW (달력 하루 리셋 대신 롤링 - 신청 하루 전 올라온 것도 유지)
    new_cutoff = (datetime.datetime.now() - datetime.timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")

    def _live_dday(r):
        # D-day 는 저장된 마감일에서 매번 계산(재수집 없이 매일 자동 감소)
        dl = r["deadline"] if "deadline" in r.keys() else None
        if dl:
            try:
                d = (datetime.date.fromisoformat(dl) - datetime.date.today()).days
                return d if d >= 0 else None
            except ValueError:
                pass
        return r["dday"]

    def _to_dict(r):
        # 표시 카테고리는 표준값으로 통일(사이트 원본이 '여행'·'식품'·빈값이어도 맛집/여가/배송/…/기타).
        cat_txt = " ".join([r["title"] or "", r["region"] or "",
                            r["category"] or "", r["channel"] or ""])
        return {
            "site": r["site"], "site_name": _SITE_NAMES.get(r["site"], r["site"]),
            "cid": r["cid"], "title": r["title"] or "", "url": r["url"] or "",
            "region": r["region"] or "", "category": category_of(r["category"], cat_txt),
            "channel": r["channel"] or "", "dday": _live_dday(r),
            "competition": r["competition"], "applicants": r["applicants"], "recruit": r["recruit"],
            "image": r["image"] if "image" in r.keys() else "",
            "first_seen": r["first_seen"] or "",
            "is_new": (r["first_seen"] or "") >= new_cutoff,
            "is_viewed": (r["site"], r["cid"]) in viewed,
            "is_fav": (r["site"], r["cid"]) in fav_set,
        }

    def _sort(items):
        if sort == "deadline":      # 마감 임박순: D-day 적게 남은 것 먼저(미상은 뒤로)
            items.sort(key=lambda d: (d["dday"] is None, d["dday"] if d["dday"] is not None else 0))
        elif sort == "competition":  # 경쟁률 낮은순(미상은 뒤로)
            items.sort(key=lambda d: (d["competition"] is None,
                                      d["competition"] if d["competition"] is not None else 0))
        elif sort == "remaining":    # 마감여유순: D-day 많이 남은 것 먼저(미상은 뒤로)
            items.sort(key=lambda d: (d["dday"] is None,
                                      -(d["dday"] if d["dday"] is not None else 0)))
        else:                        # recent = 진짜 최신순: 수집 시각(first_seen) 늦은 순
            items.sort(key=lambda d: d.get("first_seen") or "", reverse=True)
        return items

    if fav_only:
        # 찜만 보기: 사이드바 필터와 무관하게 '내가 담은 것 전부'(마감 지난 것도 포함).
        matched = [_to_dict(r) for r in db.favorites_rows(uid) if _q_ok(r)]
        _sort(matched)
        if merge:
            matched = _collapse(matched)
        new_count = sum(1 for d in matched if d["is_new"])
        page = matched[offset:offset + limit]
        return {"campaigns": page, "new_count": new_count, "total": len(matched),
                "offset": offset, "limit": limit, "has_more": offset + limit < len(matched)}

    has_filter = bool(f.get("sites") or f["keywords"] or f["regions"] or f["categories"]
                      or f["channels"] or any(f.get(k) is not None for k in NUMERIC_FILTERS))

    if not has_filter and not q and not merge:
        # 조건 없음 → DB 에서 총개수/페이지만 조회(전체 스캔 불필요, 상한 없음)
        total = db.count_seen()
        new_count = db.count_seen_new(new_cutoff)
        page = [_to_dict(r) for r in db.list_page(offset, limit, sort)]
        return {"campaigns": page, "new_count": new_count, "total": total,
                "offset": offset, "limit": limit, "has_more": offset + limit < total}

    # 필터 적용 → 활성 캠페인 전체를 훑어 매칭(사이트는 DB 레벨에서 선필터해 양을 줄임).
    # list_recent 의 최신 N건 상한을 쓰면 오래 전 수집된 사이트가 통째로 누락되므로 list_active 사용.
    # 사이드바 필터(키워드/지역/카테고리/채널/숫자)가 있을 때만 캠페인별 매칭 수행.
    # 검색·병합만 있고 필터가 없으면 매칭을 건너뛰어(전부 통과) 스캔을 빠르게.
    need_match = bool(f["keywords"] or f["regions"] or f["categories"] or f["channels"]
                      or any(f.get(k) is not None for k in NUMERIC_FILTERS))
    matched, new_count = [], 0
    for r in db.list_active(sites=f.get("sites") or None):
        if not _q_ok(r):
            continue
        if need_match:
            c = Campaign(
                site=r["site"], site_name="", cid=r["cid"], title=r["title"] or "",
                url=r["url"] or "", region=r["region"] or "", category=r["category"] or "",
                channel=r["channel"] or "", dday=_live_dday(r), applicants=r["applicants"],
                recruit=r["recruit"], competition=r["competition"],
            )
            if not matches_filter(c, f):
                continue
        d = _to_dict(r)
        if d["is_new"]:
            new_count += 1
        matched.append(d)
    _sort(matched)
    if merge:
        matched = _collapse(matched)
        new_count = sum(1 for d in matched if d["is_new"])   # 병합 후 재계산
    page = matched[offset:offset + limit]
    return {"campaigns": page, "new_count": new_count, "total": len(matched),
            "offset": offset, "limit": limit, "has_more": offset + limit < len(matched)}


@app.post("/api/view")
async def view(request: Request):
    """개별 캠페인 확인(클릭) → NEW 해제."""
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = await request.json()
    site, cid = b.get("site"), b.get("cid")
    if site and cid:
        db.mark_viewed(uid, str(site), str(cid))
    return {"ok": True}


@app.post("/api/fav")
async def fav(request: Request):
    """캠페인 찜 추가/해제(로그인 필요)."""
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = await request.json()
    site, cid = b.get("site"), b.get("cid")
    if site and cid:
        db.set_favorite(uid, str(site), str(cid), bool(b.get("on")))
    return {"ok": True}


@app.post("/api/seen-all")
async def seen_all(request: Request):
    """모두 읽음 → 수집된 캠페인 전체를 읽음(연하게) 처리."""
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = db.list_active()
    db.mark_viewed_many(uid, [(r["site"], r["cid"]) for r in rows])
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # 로그인 여부와 상관없이 항상 메인(피드)을 보여준다.
    # 비로그인이면 상단 로그인 배너 + 피드만, 로그인이면 설정까지 노출.
    # no-store: 배포 후 새 JS/CSS 가 즉시 반영되도록(브라우저 캐시로 옛 화면 고착 방지)
    return HTMLResponse(_app_html(), headers={"Cache-Control": "no-store"})


def _widget_html(size: str = "large") -> str:
    """텔레그램 로그인 위젯 마크업(로그인 버튼)."""
    if not config.BOT_USERNAME:
        return '<p style="color:#c00;font-size:13px">.env 에 TELEGRAM_BOT_USERNAME 을 넣으세요</p>'
    return (f'<script async src="https://telegram.org/js/telegram-widget.js?22" '
            f'data-telegram-login="{config.BOT_USERNAME}" data-size="{size}" '
            f'data-auth-url="/auth" data-request-access="write"></script>')


def _login_html() -> str:
    dev = ('<p style="margin-top:16px"><a href="/dev-login" '
           'style="color:#888;font-size:13px">🔧 개발용 로그인 (로컬 테스트)</a></p>'
           if config.WEB_DEV_LOGIN else "")
    widget = _widget_html("large")
    return f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>alertview</title>
<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+PHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTQiIGZpbGw9IiMzYjZlZjYiLz48cGF0aCBkPSJNMzIgMTRhMyAzIDAgMCAxIDMgM3YxLjJjNi4zIDEuMyAxMSA2LjkgMTEgMTMuNnY4LjJsMy4yIDUuM2EyIDIgMCAwIDEtMS43IDNIMTYuNWEyIDIgMCAwIDEtMS43LTNsMy4yLTUuM1YzMS44YzAtNi43IDQuNy0xMi4zIDExLTEzLjZWMTdhMyAzIDAgMCAxIDMtM3oiIGZpbGw9IiNmZmYiLz48cGF0aCBkPSJNMjYgNTBhNiA2IDAgMCAwIDEyIDB6IiBmaWxsPSIjZmZmIi8+PC9zdmc+">
<style>
 body{{font-family:-apple-system,'Malgun Gothic',sans-serif;background:#f5f6f8;margin:0}}
 main{{max-width:440px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;
   align-items:center;justify-content:center;text-align:center;padding:24px}}
 h1{{font-size:24px;margin:0 0 8px}} p{{color:#555;line-height:1.6}}
 .card{{background:#fff;border-radius:16px;padding:32px 24px;box-shadow:0 2px 16px rgba(0,0,0,.06);width:100%}}
</style></head><body><main><div class=card>
<h1>🔔 체험단 알림</h1>
<p>새 체험단이 내 조건에 맞게 뜨면<br>텔레그램으로 바로 알려드려요.</p>
<div style="margin-top:20px;display:flex;justify-content:center">{widget}</div>{dev}</div></main></body></html>"""


def _app_html() -> str:
    return _APP_HTML.replace("__LOGIN_WIDGET__", _widget_html("medium"))


_APP_HTML = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>alertview</title>
<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+PHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTQiIGZpbGw9IiMzYjZlZjYiLz48cGF0aCBkPSJNMzIgMTRhMyAzIDAgMCAxIDMgM3YxLjJjNi4zIDEuMyAxMSA2LjkgMTEgMTMuNnY4LjJsMy4yIDUuM2EyIDIgMCAwIDEtMS43IDNIMTYuNWEyIDIgMCAwIDEtMS43LTNsMy4yLTUuM1YzMS44YzAtNi43IDQuNy0xMi4zIDExLTEzLjZWMTdhMyAzIDAgMCAxIDMtM3oiIGZpbGw9IiNmZmYiLz48cGF0aCBkPSJNMjYgNTBhNiA2IDAgMCAwIDEyIDB6IiBmaWxsPSIjZmZmIi8+PC9zdmc+">
<style>
 :root{
   --blue:#3b6ef6;--blue-d:#2b59d6;
   --ink:#1f2533;--ink2:#586173;--muted:#97a1b2;
   --line:#e7ebf2;--bg:#eef1f7;--card:#fff;
   --good:#15a34a;--good-bg:#e7f7ec;
   --r:16px;--r-sm:11px;
   --sh:0 1px 2px rgba(22,30,55,.04),0 12px 30px -18px rgba(22,30,55,.20);
 }
 *{box-sizing:border-box}
 html,body{height:100%}
 body{margin:0;color:var(--ink);background:var(--bg);
   font-family:-apple-system,BlinkMacSystemFont,'Malgun Gothic','Apple SD Gothic Neo',sans-serif;
   -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
 main{max-width:1240px;margin:0 auto;padding:18px 16px 14px;height:100vh;display:flex;flex-direction:column}
 .layout{display:flex;gap:16px;align-items:stretch;flex:1;min-height:0}
 .col-left{flex:0 0 358px;max-width:358px;overflow-y:auto;min-height:0;padding-right:6px;scrollbar-width:thin;scrollbar-color:#d3d9e3 transparent}
 .col-right{flex:1;min-width:0;min-height:0}
 .col-right>.card{height:100%;display:flex;flex-direction:column;margin-bottom:0}
 .col-left::-webkit-scrollbar,#feedwrap::-webkit-scrollbar{width:8px}
 .col-left::-webkit-scrollbar-thumb,#feedwrap::-webkit-scrollbar-thumb{background:#d3d9e3;border-radius:8px}
 .col-left::-webkit-scrollbar-thumb:hover,#feedwrap::-webkit-scrollbar-thumb:hover{background:#bdc6d4}
 @media(max-width:860px){
   main{height:auto;display:block}
   .layout{flex-direction:column;height:auto;min-height:0}
   .col-left{flex:none;max-width:none;width:100%;overflow:visible;padding-right:0}
   .col-right{width:100%}
   .col-right>.card{height:auto}
   #feedwrap{max-height:calc(100vh - 170px)}
 }
 header{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
 h1{font-size:21px;font-weight:800;letter-spacing:-.02em;margin:0}
 .sub{color:var(--ink2);font-size:13px;margin:0 0 16px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:18px;box-shadow:var(--sh);margin-bottom:14px}
 .card h2{font-size:14px;font-weight:700;letter-spacing:-.01em;margin:0 0 12px}
 .row{display:flex;gap:8px}
 input[type=text],input[type=number]{flex:1;width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:var(--r-sm);font-size:15px;background:#fbfcfe;transition:border-color .15s,box-shadow .15s}
 input[type=text]:focus,input[type=number]:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(59,110,246,.15);background:#fff}
 button{cursor:pointer;border:none;border-radius:var(--r-sm);font-size:14px;transition:transform .05s,background .15s,box-shadow .15s}
 button:active{transform:translateY(1px)}
 .add{background:var(--blue);color:#fff;padding:0 16px;font-weight:700}
 .add:hover{background:var(--blue-d)}
 .chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
 .chip{background:#eef3ff;color:#2f4d9e;border-radius:999px;padding:6px 12px;font-size:13.5px;display:flex;align-items:center;gap:7px}
 .chip b{cursor:pointer;color:#9aa8cf;font-weight:800;font-size:15px;line-height:1}
 .chip b:hover{color:#5b6ea8}
 .opts{display:flex;flex-wrap:wrap;gap:7px}
 .opt{padding:8px 14px;border:1px solid var(--line);border-radius:999px;background:#fff;font-size:13.5px;color:var(--ink2);transition:all .15s}
 .opt:hover{border-color:#c4cdde;background:#f7f9fc}
 .opt.on{background:var(--blue);color:#fff;border-color:var(--blue);font-weight:600}
 .opt.on:hover{background:var(--blue-d)}
 .gubox{margin-top:8px;padding:11px 12px;background:#f5f8ff;border:1px solid #e4ebfa;border-left:3px solid var(--blue);border-radius:12px}
 .guhd{font-size:12px;color:#5566a0;font-weight:700;margin-bottom:8px}
 .opt.sub{font-size:13px;padding:6px 11px;border-style:dashed}
 .opt.sub.on{border-style:solid;background:var(--blue);color:#fff;border-color:var(--blue)}
 .muted{color:var(--muted);font-size:12.5px;margin-top:8px;line-height:1.5}
 .top{display:flex;align-items:center;justify-content:space-between}
 .switch{position:relative;width:48px;height:28px}
 .switch input{display:none}
 .slider{position:absolute;inset:0;background:#ccd2dc;border-radius:28px;transition:.2s}
 .slider:before{content:"";position:absolute;width:22px;height:22px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
 .switch input:checked+.slider{background:#34c759}
 .switch input:checked+.slider:before{transform:translateX(20px)}
 a.logout{color:var(--muted);font-size:13px;text-decoration:none}
 a.logout:hover{color:var(--ink2)}
 #feedwrap{flex:1;min-height:0;overflow-y:auto;margin-top:12px;padding-right:2px;scrollbar-width:thin;scrollbar-color:#d3d9e3 transparent}
 #feed{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:12px}
 .item{position:relative;display:block;border:1px solid var(--line);border-radius:14px;overflow:hidden;text-decoration:none;color:var(--ink);background:#fff;transition:transform .12s,box-shadow .12s,border-color .12s}
 .item:hover{transform:translateY(-2px);box-shadow:0 10px 24px -12px rgba(22,30,55,.28);border-color:#dde3ee}
 .item.viewed{opacity:.45}
 .thumb{width:100%;aspect-ratio:4/3;object-fit:cover;display:block;background:#eef0f3}
 .thumb.ph{display:flex;align-items:center;justify-content:center;color:#cdd4de;font-size:26px;background:linear-gradient(135deg,#f3f5f9,#e8edf4)}
 .body{padding:9px 10px 11px}
 .item .t{font-size:12.5px;font-weight:600;line-height:1.34;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:34px}
 .item .m{font-size:11px;color:var(--muted);margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .pills{display:flex;flex-wrap:wrap;gap:4px;margin-top:7px}
 .pill{font-size:10.5px;background:#f1f4f8;color:#5a6573;border-radius:7px;padding:2px 7px;font-weight:500}
 .pill.good{background:var(--good-bg);color:var(--good);font-weight:700}
 .pill.site{background:#efeafe;color:#6b46c1;font-weight:600}
 .badge{position:absolute;top:7px;left:7px;background:#ff3b30;color:#fff;font-size:9.5px;font-weight:800;letter-spacing:.02em;border-radius:7px;padding:3px 7px;z-index:1;box-shadow:0 2px 6px rgba(255,59,48,.4)}
 .seenbtn{background:#eef3ff;color:#2f4d9e;padding:8px 13px;font-weight:600}
 .seenbtn:hover{background:#e0e9ff}
 .feedctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .sortsel{padding:8px 10px;border:1px solid var(--line);border-radius:var(--r-sm);font-size:13px;background:#fff;color:var(--ink2);cursor:pointer}
 #favtgl.on{background:#ffe3e6;color:#e0354b}
 .clearbtn{font-size:12px;font-weight:600;color:#e0354b;background:#ffe3e6;border:none;border-radius:8px;padding:5px 10px;cursor:pointer}
 .clearbtn:hover{background:#ffd0d6}
 .savebtn{margin-top:8px;background:#eef3ff;color:#2f4d9e;font-weight:700;padding:8px 12px;border-radius:10px;font-size:13px}
 .savebtn:hover{background:#e0e9ff}
 .preset-chip{background:#fff5e9;color:#b45a09;cursor:pointer;font-weight:600}
 .preset-chip b{color:#d39a5e;font-weight:800}
 .favbtn{position:absolute;top:7px;right:7px;z-index:2;width:30px;height:30px;padding:0;border:none;border-radius:50%;background:rgba(255,255,255,.92);color:#b6bdc8;font-size:15px;line-height:30px;text-align:center;cursor:pointer;box-shadow:0 1px 5px rgba(0,0,0,.18)}
 .favbtn:hover{background:#fff;transform:scale(1.08)}
 .favbtn.on{color:#ff3b30}
 #newbanner{width:100%;background:var(--blue);color:#fff;font-weight:700;padding:11px;border-radius:12px;margin-top:10px;font-size:13px;border:none;cursor:pointer;box-shadow:0 6px 16px -6px rgba(59,110,246,.55)}
 #newbanner:hover{background:var(--blue-d)}
 .num{display:flex;align-items:center;gap:8px;margin-bottom:10px}
 .num label{width:52px;font-size:14px;color:var(--ink2);flex:none}
 .num input{padding:9px 10px;font-size:14px;text-align:center}
 .tilde{color:var(--ink2);font-size:13px;flex:none}
 .preset{background:#fff5e9;color:#b45a09;border:1px solid #ffdcb0;padding:8px 13px;border-radius:999px;font-size:13px;font-weight:600}
 .preset:hover{background:#ffeed7}
 #loginbar{background:linear-gradient(135deg,#3b6ef6,#5a86f8);color:#fff;border-radius:18px;padding:20px;margin-bottom:16px;text-align:center;box-shadow:0 14px 32px -12px rgba(45,108,223,.55)}
 #loginbar h2{font-size:17px;font-weight:800;margin:0 0 5px}
 #loginbar p{color:#e7eefc;font-size:13px;margin:0 0 13px;line-height:1.5}
 #loginbar .wrap{display:flex;justify-content:center;min-height:40px}
</style></head><body><main>
<header><h1>🔔 체험단 알림</h1><span style="display:flex;gap:14px;align-items:center"><a class=logout href="/map">🗺 지도</a><a class=logout href="/inquiry">💬 문의</a><a class=logout href="/logout" id=logoutlink style="display:none">로그아웃</a></span></header>
<p class=sub id=hello></p>

<div id=loginbar style="display:none">
  <h2>🔔 알림을 받아보세요</h2>
  <p>내 조건에 맞는 새 체험단이 뜨면<br>텔레그램으로 바로 알려드려요.</p>
  <div class=wrap>__LOGIN_WIDGET__</div>
</div>

<div class=layout>
<div class=col-left>
<div id=notifcard style="display:none">
<div class=card><div class=top>
  <h2 style="margin:0">알림 받기</h2>
  <label class=switch><input type=checkbox id=active onchange="toggleActive()"><span class=slider></span></label>
</div><p class=muted>끄면 새 캠페인이 떠도 알림이 오지 않아요.</p></div>
</div>

<div class=card>
  <h2 style="display:flex;justify-content:space-between;align-items:center">⭐ 내 검색 조건
    <button class=clearbtn onclick="clearAll()">🧹 전체 해제</button></h2>
  <div class=guhd style="margin-bottom:8px">저장한 조건을 한 번에 적용</div>
  <div class=chips id=presets></div>
  <button class=savebtn onclick="savePreset()">＋ 지금 조건 저장</button>
</div>

<div class=card>
  <h2 style="display:flex;justify-content:space-between;align-items:center">체험단
    <button class=clearbtn id=clrsites onclick="clearFilter('site','sites')" style="display:none">전체 해제</button></h2>
  <div class=opts id=sites></div>
  <p class=muted>보고 싶은 체험단만 선택. 안 고르면 전체.</p>
</div>

<div class=card>
  <h2 style="display:flex;justify-content:space-between;align-items:center">키워드
    <button class=clearbtn id=clrkw onclick="clearFilter('keyword','keywords')" style="display:none">전체 해제</button></h2>
  <div class=row>
    <input type=text id=kw placeholder="예: 오마카세, 횡성" onkeydown="if(event.key==='Enter')addKw()">
    <button class=add onclick="addKw()">추가</button>
  </div>
  <div class=chips id=kwchips></div>
  <p class=muted>키워드는 하나라도 맞으면 알림 (예: 횡성 또는 평창).</p>
</div>

<div class=card>
  <h2 style="display:flex;justify-content:space-between;align-items:center">카테고리
    <button class=clearbtn id=clrcat onclick="clearFilter('category','categories')" style="display:none">전체 해제</button></h2>
  <div class=opts id=categories></div>
  <p class=muted>고른 카테고리만. 안 고르면 전체.</p>
</div>

<div class=card>
  <h2 style="display:flex;justify-content:space-between;align-items:center">채널 (콘텐츠 유형)
    <button class=clearbtn id=clrch onclick="clearFilter('channel','channels')" style="display:none">전체 해제</button></h2>
  <div class=opts id=channels></div>
  <p class=muted>콘텐츠 유형으로 필터. 고른 채널만, 안 고르면 전체.</p>
</div>

<div class=card>
  <h2 style="display:flex;justify-content:space-between;align-items:center">지역
    <button class=clearbtn id=regclear onclick="clearRegions()" style="display:none">전체 해제</button></h2>
  <div class=opts id=sidos></div>
  <div id=gus></div>
  <div class=chips id=regchips></div>
  <p class=muted>시/도를 누르면 구 단위로 세분화해 고를 수 있어요. '○○ 전체'를 고르면 그 시/도 전부. 키워드와 같이 쓰면 둘 다 맞아야 알림.</p>
</div>

<div class=card>
  <h2>상세 (당첨 확률)</h2>
  <div class=num><label>경쟁률</label>
    <input type=number step=0.1 min=0 id=mincomp placeholder=최소 onchange="saveNum('min_competition','mincomp')">
    <span class=tilde>~</span>
    <input type=number step=0.1 min=0 id=maxcomp placeholder=최대 onchange="saveNum('max_competition','maxcomp')"></div>
  <div class=num><label>마감일</label>
    <input type=number min=0 id=mindday placeholder=최소 onchange="saveNum('min_dday','mindday')">
    <span class=tilde>~</span>
    <input type=number min=0 id=maxdday placeholder=최대 onchange="saveNum('max_dday','maxdday')"></div>
  <div class=num><label>모집수</label>
    <input type=number min=0 id=minrec placeholder=최소 onchange="saveNum('min_recruit','minrec')">
    <span class=tilde>~</span>
    <input type=number min=0 id=maxrec placeholder=최대 onchange="saveNum('max_recruit','maxrec')"></div>
  <div class=num><label>지원수</label>
    <input type=number min=0 id=minapp placeholder=최소 onchange="saveNum('min_applicants','minapp')">
    <span class=tilde>~</span>
    <input type=number min=0 id=maxapp placeholder=최대 onchange="saveNum('max_applicants','maxapp')"></div>
  <p class=muted>비워둔 칸은 제한 없음. 예) 경쟁률 0~2, 마감일 3~10일. 경쟁률=신청÷모집(낮을수록 당첨↑), 모집수 많고 지원수 적을수록 유리.</p>
</div>
</div>
<div class=col-right>
<div class=card>
  <div class=top><h2 style="margin:0">📋 캠페인 <span id=cnt class=muted></span></h2>
    <div class=feedctl>
      <select id=sortsel class=sortsel onchange="changeSort()">
        <option value=recent>최신순(수집순)</option>
        <option value=remaining>마감여유순</option>
        <option value=deadline>마감임박순</option>
        <option value=competition>경쟁률↓</option>
      </select>
      <button id=mergetgl class="seenbtn on" onclick="toggleMerge()" title="같은 업체 중복(채널변형 등) 묶기">🔁 병합</button>
      <button id=favtgl class=seenbtn onclick="toggleFavOnly()" style="display:none">♡ 찜</button>
      <button class=seenbtn onclick="seenAll()">모두 읽음</button>
    </div></div>
  <div style="display:flex;gap:8px;margin-top:10px">
    <input id=searchbox type=search placeholder="🔍 업체명·지역·제목 검색"
       style="flex:1;padding:9px 12px;border:1px solid var(--line);border-radius:var(--r-sm);font-size:14px;color:var(--ink)"
       onkeydown="if(event.key==='Enter')doSearch()" oninput="if(!this.value)doSearch()">
    <button class=seenbtn onclick="doSearch()">검색</button>
  </div>
  <button id=newbanner onclick="showNew()" style="display:none"></button>
  <div id=feedwrap><div id=feed></div></div>
</div>
</div>
</div>
</main>
<script>
let S={}, guest=false;
let curSido=null;   // 지역: 현재 펼친 시/도
// 숫자 범위 필터: [상태키, 짧은이름(=쿼리파라미터=input id)]. 저장·URL·렌더·해제가 이 목록 하나를 공유.
const NUMF=[['min_competition','mincomp'],['max_competition','maxcomp'],
            ['min_dday','mindday'],['max_dday','maxdday'],
            ['min_recruit','minrec'],['max_recruit','maxrec'],
            ['min_applicants','minapp'],['max_applicants','maxapp']];
async function load(){
  const r=await fetch('/api/state');
  S=await r.json();
  guest=!S.logged_in;
  document.getElementById('loginbar').style.display=guest?'':'none';
  document.getElementById('notifcard').style.display=guest?'none':'';
  document.getElementById('logoutlink').style.display=guest?'none':'';
  document.getElementById('favtgl').style.display=guest?'none':'';  // 찜은 로그인 사용자만
  const u=new URLSearchParams(location.search);          // URL 로 정렬·찜 지정 가능(공유/북마크)
  if(['recent','remaining','deadline','competition'].includes(u.get('sort'))) feedSort=u.get('sort');
  favOnly=(u.get('fav')==='1') && !guest;
  const uq=u.get('q'); if(uq){ searchQ=uq; const sb=document.getElementById('searchbox'); if(sb) sb.value=uq; }  // ?q= 검색 진입
  const only=u.get('only');   // 지도에서 지역 클릭: 다른 필터 전부 초기화하고 그 지역만
  if(only){
    searchQ=only; const sb2=document.getElementById('searchbox'); if(sb2) sb2.value=only;
    ['sites','keywords','regions','categories','channels'].forEach(k=>{ if(Array.isArray(S[k])) S[k]=[]; });
    NUMF.forEach(([k])=>{ if(k in S) S[k]=null; });
    if(!guest){ try{ await post('/api/clear',{type:'all'}); }catch(e){} }
  }
  // only/q 는 '한 번 진입'용 → 주소창에서 제거(새로고침 시 되살아나지 않게, 검색 지우면 실제로 지워지게)
  if(only||uq){ try{ history.replaceState({}, '', location.pathname); }catch(e){} }
  document.getElementById('sortsel').value=feedSort;     // 드롭다운에 현재 정렬 반영
  const fb=document.getElementById('favtgl');
  fb.classList.toggle('on',favOnly); fb.textContent=favOnly?'♥ 찜만':'♡ 찜';
  render();
  loadCampaigns();
}
function refreshLocal(){ render(); loadCampaigns(); }   // 게스트: 서버 저장 없이 화면만 갱신
function toggleArr(arr,v){const i=arr.indexOf(v); if(i>=0)arr.splice(i,1); else arr.push(v);}
let feedOffset=0, feedTotal=0, feedLoading=false, feedDone=false;
let feedSort='recent', favOnly=false;
const viewedLocal=new Set();   // 이번 세션에서 클릭(읽음)한 항목 → 자동 새로고침에도 유지
const loadedKeys=new Set();     // 현재 피드에 표시된 캠페인 키 → 새 항목 감지용
const vkey=(s,c)=>s+'|'+c;
function feedParams(){
  const p=new URLSearchParams();
  if(guest){
    if(S.sites&&S.sites.length)p.set('sites',S.sites.join(','));
    if(S.keywords.length)p.set('kw',S.keywords.join(','));
    if(S.regions.length)p.set('region',S.regions.join(','));
    if(S.categories.length)p.set('cat',S.categories.join(','));
    if(S.channels.length)p.set('ch',S.channels.join(','));
    NUMF.forEach(([sk,sn])=>{ if(S[sk]!=null)p.set(sn,S[sk]); });
  }
  p.set('sort',feedSort);
  if(favOnly)p.set('fav','1');
  if(searchQ)p.set('q',searchQ);
  if(!mergeOn)p.set('merge','0');
  return p;
}
let searchQ='';
function doSearch(){ searchQ=(document.getElementById('searchbox').value||'').trim(); loadCampaigns(true); }
let mergeOn=true;
function toggleMerge(){ mergeOn=!mergeOn; document.getElementById('mergetgl').classList.toggle('on',mergeOn); loadCampaigns(true); }
function resetAll(){ searchQ=''; const s=document.getElementById('searchbox'); if(s)s.value=''; clearAll(); }
function changeSort(){ feedSort=document.getElementById('sortsel').value; loadCampaigns(true); }
function toggleFavOnly(){
  favOnly=!favOnly;
  const b=document.getElementById('favtgl');
  b.classList.toggle('on',favOnly); b.textContent=favOnly?'♥ 찜만':'♡ 찜';
  loadCampaigns(true);
}
async function toggleFav(site,cid,btn){
  const on=!btn.classList.contains('on');
  btn.classList.toggle('on',on); btn.textContent=on?'♥':'♡';
  await post('/api/fav',{site,cid,on});
  if(favOnly && !on){ const a=btn.closest('.item'); if(a) a.remove(); }  // 찜 목록에서 해제 시 즉시 제거
}
async function loadCampaigns(reset=true){
  if(feedLoading)return; feedLoading=true;
  if(reset){feedOffset=0; feedDone=false; loadedKeys.clear();
    document.getElementById('feed').innerHTML='';
    const nb=document.getElementById('newbanner'); if(nb) nb.style.display='none';}
  const p=feedParams(); p.set('offset',feedOffset); p.set('limit',60);
  const r=await fetch('/api/campaigns?'+p.toString());
  if(r.status===401){feedLoading=false;return;}
  const d=await r.json();
  feedTotal=d.total;
  document.getElementById('cnt').textContent='\u00b7 '+d.total+'\uac1c'+(d.new_count?(' (NEW '+d.new_count+')'):'');
  appendFeed(d.campaigns);
  feedOffset+=d.campaigns.length;
  feedDone=!d.has_more;
  if(reset && !d.campaigns.length){
    document.getElementById('feed').innerHTML=
      '<div class=muted style="grid-column:1/-1;text-align:center;padding:36px 12px;line-height:1.7">'+
      '\uc120\ud0dd\ud55c \ud544\ud130\uc5d0 \ub9de\ub294 \ucea0\ud398\uc778\uc774 \uc5c6\uc5b4\uc694.<br>'+
      '\uc9c0\uc5ed\u00b7\uce74\ud14c\uace0\ub9ac\u00b7\uc0ac\uc774\ud2b8\u00b7\ucc44\ub110 \ub4f1 \uc5ec\ub7ec \uc870\uac74\uc774 \uac19\uc774 \uac78\ub824 \uc788\uc744 \uc218 \uc788\uc5b4\uc694.<br>'+
      '<button class=clearbtn style="margin-top:14px" onclick="resetAll()">\ud544\ud130 \uc804\uccb4 \ud574\uc81c</button></div>';
  }
  feedLoading=false;
}
function esc(s){return (s||'').replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
function appendFeed(arr){
  const f=document.getElementById('feed');
  arr.forEach(c=>{
    loadedKeys.add(vkey(c.site,c.cid));
    const a=document.createElement('a'); a.className='item'+((c.is_viewed||viewedLocal.has(vkey(c.site,c.cid)))?' viewed':'');
    a.href=c.url; a.target='_blank'; a.rel='noopener';
    const meta=[c.region,c.category,c.channel].filter(Boolean).join(' \u00b7 ');
    let pills='';
    if(c.site_name) pills+='<span class="pill site">'+esc(c.site_name)+'</span>';
    if(c.dup_count>1) pills+='<span class="pill" title="묶음: '+(c.dup_sites?esc(c.dup_sites.join(', ')):'')+'">🔁 '+c.dup_count+'</span>';
    if(c.dday!=null) pills+='<span class="pill'+(c.dday<=1?' good':'')+'">'+(c.dday===0?'오늘마감':'D-'+c.dday)+'</span>';
    if(c.applicants!=null||c.recruit!=null) pills+='<span class=pill>\uc2e0\uccad '+(c.applicants!=null?c.applicants:'-')+' / \ubaa8\uc9d1 '+(c.recruit!=null?c.recruit:'-')+'</span>';
    if(c.competition!=null) pills+='<span class="pill'+(c.competition<=1?' good':'')+'">\uacbd\uc7c1\ub960 '+c.competition+'</span>';
    a.innerHTML=(c.is_new?'<span class=badge>NEW</span>':'')+
      '<div class="thumb ph">🍽️</div>'+
      '<div class=body><div class=t>'+esc(c.title)+'</div><div class=m>'+esc(meta)+'</div>'+
      (pills?'<div class=pills>'+pills+'</div>':'')+'</div>';
    if(c.image){
      const img=document.createElement('img'); img.className='thumb'; img.loading='lazy';
      img.addEventListener('error',()=>{img.replaceWith(makePh());});
      img.src=c.image;
      const ph=a.querySelector('.thumb.ph'); if(ph) ph.replaceWith(img);
    }
    if(!guest){
      const fb=document.createElement('button');
      fb.className='favbtn'+(c.is_fav?' on':''); fb.textContent=c.is_fav?'♥':'♡'; fb.title='찜';
      fb.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();toggleFav(c.site,c.cid,fb);});
      a.appendChild(fb);
    }
    a.addEventListener('click',()=>markView(c.site,c.cid,a));
    f.appendChild(a);
  });
}
function makePh(){const d=document.createElement('div');d.className='thumb ph';d.textContent='🍽️';return d;}
async function markView(site,cid,el){
  if(el.classList.contains('viewed'))return;
  el.classList.add('viewed');               // \uc77d\uc74c
  viewedLocal.add(vkey(site,cid));          // \uc790\ub3d9 \uc0c8\ub85c\uace0\uce68\uc5d0\ub3c4 \uc77d\uc74c \uc720\uc9c0
  if(!guest) fetch('/api/view',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({site,cid})});
}
async function seenAll(){
  if(!guest) await fetch('/api/seen-all',{method:'POST'});   // 로그인 시 서버에도 저장
  loadedKeys.forEach(k=>viewedLocal.add(k));                  // 게스트 포함: 현재 목록 읽음 처리
  document.querySelectorAll('#feed .item').forEach(el=>el.classList.add('viewed'));
}
function showNew(){
  document.getElementById('newbanner').style.display='none';
  const w=document.getElementById('feedwrap'); if(w) w.scrollTop=0;
  loadCampaigns(true);
}
async function checkNew(){   // 목록을 갈아끼우지 않고 새 캠페인이 있는지만 확인
  if(feedLoading) return;
  const p=feedParams(); p.set('offset',0); p.set('limit',60);
  let d;
  try{ const r=await fetch('/api/campaigns?'+p.toString()); if(!r.ok) return; d=await r.json(); }
  catch(e){ return; }
  let n=0;
  for(const c of d.campaigns){ if(loadedKeys.has(vkey(c.site,c.cid))) break; n++; }
  const b=document.getElementById('newbanner');
  if(!b) return;
  if(n>0){ b.textContent='🆕 새 캠페인 '+n+'개 — 보기'; b.style.display='block'; }
}
(function(){const w=document.getElementById('feedwrap'); if(!w)return;
  w.addEventListener('scroll',function(){
    if(feedLoading||feedDone)return;
    if(w.scrollTop+w.clientHeight>=w.scrollHeight-120) loadCampaigns(false);
  });
})();
function chips(elId,arr,delFn){
  const c=document.getElementById(elId); c.innerHTML='';
  if(!arr.length){c.innerHTML='<span class=muted>아직 없음 (없으면 전체 수신)</span>';return;}
  arr.forEach(k=>{const e=document.createElement('span');e.className='chip';
    e.innerHTML=k+' <b>×</b>';e.querySelector('b').onclick=()=>delFn(k);c.appendChild(e);});
}
function opts(elId,all,selected,toggleFn){
  const c=document.getElementById(elId); c.innerHTML='';
  all.forEach(v=>{const on=selected.includes(v);
    const b=document.createElement('button');b.className='opt'+(on?' on':'');b.textContent=v;
    b.onclick=()=>toggleFn(v);c.appendChild(b);});
}
function optBtn(label,on,fn){
  const b=document.createElement('button');b.className='opt'+(on?' on':'');
  b.textContent=label;b.onclick=fn;return b;
}
function renderRegions(){
  const tree=S.region_tree||{};
  const sel=S.regions||[];
  const rc=document.getElementById('regclear'); if(rc) rc.style.display=sel.length?'':'none';
  // 1) 시/도 버튼 (선택된 구가 있는 시/도는 점으로 표시)
  const sc=document.getElementById('sidos'); sc.innerHTML='';
  Object.keys(tree).forEach(sd=>{
    if(sd==='기타'){   // 기타는 세부(구) 없이 단일 선택
      const b=optBtn('기타'+(sel.includes('기타')?' •':''), sel.includes('기타'), ()=>pickRegion('기타'));
      b.classList.add('sido'); sc.appendChild(b); return;
    }
    const picked=sel.some(r=>r===sd||r.startsWith(sd+' '));
    const b=optBtn(sd+(picked?' •':''), curSido===sd, ()=>{
      curSido=(curSido===sd?null:sd); renderRegions();
    });
    b.classList.add('sido');
    sc.appendChild(b);
  });
  // 2) 펼친 시/도의 구 목록(별도 박스로 구분)
  const gc=document.getElementById('gus'); gc.innerHTML='';
  if(curSido){
    const box=document.createElement('div'); box.className='gubox';
    const hd=document.createElement('div'); hd.className='guhd';
    hd.textContent='📍 '+curSido+' 세부 지역 선택';
    box.appendChild(hd);
    const wrap=document.createElement('div'); wrap.className='opts';
    const allB=optBtn(curSido+' 전체', sel.includes(curSido), ()=>pickRegion(curSido));
    allB.classList.add('sub');
    wrap.appendChild(allB);
    (tree[curSido]||[]).forEach(gu=>{
      const val=curSido+' '+gu;
      const b=optBtn(gu, sel.includes(val), ()=>pickRegion(val));
      b.classList.add('sub');
      wrap.appendChild(b);
    });
    box.appendChild(wrap);
    gc.appendChild(box);
  }
  // 3) 선택된 지역 칩
  chips('regchips', sel, delRegion);
}
function pickRegion(v){
  let regs=(S.regions||[]).slice();
  if(regs.includes(v)){
    regs=regs.filter(x=>x!==v);            // 이미 선택 → 해제
  } else if(v.indexOf(' ')<0){
    // 시/도 '전체' 선택 → 같은 시/도의 구들 자동 해제
    regs=regs.filter(x=>!x.startsWith(v+' '));
    regs.push(v);
  } else {
    // 특정 구 선택 → 같은 시/도의 '전체' 자동 해제
    const sido=v.split(' ')[0];
    regs=regs.filter(x=>x!==sido);
    regs.push(v);
  }
  saveRegions(regs);
}
function saveRegions(regs){
  S.regions=regs;
  if(guest){refreshLocal();return;}
  post('/api/region/set',{values:regs}).then(load);
}
function clearRegions(){ curSido=null; saveRegions([]); }   // 지역 전체 해제
async function clearFilter(type,key){   // 특정 필터 전체 해제(체험단/키워드/카테고리/채널)
  S[key]=[];
  if(guest){refreshLocal();return;}
  await post('/api/clear',{type});await load();
}
async function clearAll(){               // 완전 전체 해제(모든 조건)
  searchQ=''; {const sb=document.getElementById('searchbox'); if(sb) sb.value='';}   // 검색어도 초기화
  S.sites=[];S.keywords=[];S.regions=[];S.categories=[];S.channels=[];
  NUMF.forEach(([sk])=>S[sk]=null);curSido=null;
  if(guest){refreshLocal();return;}
  await post('/api/clear',{type:'all'});await load();
}
function _showClear(id,has){const e=document.getElementById(id); if(e) e.style.display=has?'':'none';}
// 저장된 검색 조건(프리셋)
function guestPresets(){ try{return JSON.parse(localStorage.getItem('presets')||'{}');}catch(e){return {};} }
function presetList(){
  if(!guest) return (S.presets||[]);
  const o=guestPresets(); return Object.keys(o).map(n=>({name:n,payload:o[n]}));
}
function curPayload(){
  const p={sites:S.sites||[],keywords:S.keywords||[],regions:S.regions||[],
           categories:S.categories||[],channels:S.channels||[]};
  NUMF.forEach(([sk])=>p[sk]=(S[sk]??null));
  return p;
}
function renderPresets(){
  const c=document.getElementById('presets'); if(!c)return; c.innerHTML='';
  const list=presetList();
  if(!list.length){ c.innerHTML='<span class=muted>저장된 조건이 없어요. 조건을 고른 뒤 아래로 저장하세요.</span>'; return; }
  list.forEach(p=>{
    const e=document.createElement('span'); e.className='chip preset-chip';
    const t=document.createElement('span'); t.textContent=p.name; t.onclick=()=>applyPreset(p.name,p.payload);
    const x=document.createElement('b'); x.textContent=' ×'; x.title='삭제';
    x.onclick=(ev)=>{ev.stopPropagation();delPreset(p.name);};
    e.appendChild(t); e.appendChild(x); c.appendChild(e);
  });
}
async function savePreset(){
  const name=(prompt('이 검색 조건의 이름을 정해주세요 (예: 강원 맛집 블로그)')||'').trim();
  if(!name)return;
  const payload=curPayload();
  if(guest){const o=guestPresets();o[name]=payload;localStorage.setItem('presets',JSON.stringify(o));renderPresets();return;}
  await post('/api/preset/save',{name,payload}); await load();
}
async function applyPreset(name,payload){
  searchQ=''; {const sb=document.getElementById('searchbox'); if(sb) sb.value='';}   // 프리셋 적용 시 검색어 초기화
  S.sites=payload.sites||[];S.keywords=payload.keywords||[];S.regions=payload.regions||[];
  S.categories=payload.categories||[];S.channels=payload.channels||[];
  NUMF.forEach(([sk])=>S[sk]=(payload[sk]??null));curSido=null;
  if(guest){refreshLocal();return;}
  await post('/api/preset/apply',{name}); await load();
}
async function delPreset(name){
  if(guest){const o=guestPresets();delete o[name];localStorage.setItem('presets',JSON.stringify(o));renderPresets();return;}
  await post('/api/preset/delete',{name}); await load();
}
function delRegion(v){ pickRegion(v); }
function renderSites(){
  const c=document.getElementById('sites'); if(!c)return; c.innerHTML='';
  (S.all_sites||[]).forEach(s=>{
    c.appendChild(optBtn(s.name, (S.sites||[]).includes(s.key), ()=>toggleSite(s.key)));
  });
}
async function toggleSite(v){
  if(guest){toggleArr(S.sites,v);refreshLocal();return;}
  await post('/api/site/toggle',{value:v});await load();
}
function render(){
  document.getElementById('hello').textContent = guest
    ? '키워드·지역으로 검색해 보세요. 알림으로 받고 싶으면 로그인하세요.'
    : '설정은 자동 저장돼요.';
  document.getElementById('active').checked=!!S.active;
  renderSites();
  chips('kwchips',S.keywords,delKw);
  opts('categories',S.all_categories,S.categories,toggleCategory);
  opts('channels',S.all_channels,S.channels,toggleChannel);
  renderRegions();
  _showClear('clrsites',(S.sites||[]).length);
  _showClear('clrkw',(S.keywords||[]).length);
  _showClear('clrcat',(S.categories||[]).length);
  _showClear('clrch',(S.channels||[]).length);
  renderPresets();
  NUMF.forEach(([sk,sn])=>{const e=document.getElementById(sn); if(e)e.value=(S[sk]??'');});
}
async function post(u,b){await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});}
async function addKw(){const i=document.getElementById('kw');const v=i.value.trim();if(!v)return;
  i.value='';
  if(guest){if(!S.keywords.includes(v))S.keywords.push(v);refreshLocal();return;}
  await post('/api/keyword/add',{value:v});await load();}
async function delKw(v){
  if(guest){S.keywords=S.keywords.filter(k=>k!==v);refreshLocal();return;}
  await post('/api/keyword/del',{value:v});await load();}
async function toggleRegion(v){
  if(guest){toggleArr(S.regions,v);refreshLocal();return;}
  await post('/api/region/toggle',{value:v});await load();}
async function toggleCategory(v){
  if(guest){toggleArr(S.categories,v);refreshLocal();return;}
  await post('/api/category/toggle',{value:v});await load();}
async function toggleChannel(v){
  if(guest){toggleArr(S.channels,v);refreshLocal();return;}
  await post('/api/channel/toggle',{value:v});await load();}
async function toggleActive(){await post('/api/active',{active:document.getElementById('active').checked});}
async function saveNum(key,elId){const raw=document.getElementById(elId).value;const v=(raw===''?null:raw);
  if(guest){S[key]=(v===null?null:parseFloat(v));refreshLocal();return;}
  await post('/api/scalar',{key:key,value:v});await load();}
load();
setInterval(checkNew, 30000);   // 30초마다 새 캠페인 유무만 확인(목록은 그대로)
// D-day 는 서버가 마감일-오늘로 매번 계산하지만, 탭을 켜둔 채 날짜가 바뀌면 화면값이 안 바뀐다.
// 자정을 넘겼거나(날짜 변경) 탭에 다시 돌아오면 목록을 다시 그려 D-day 를 최신으로.
let _loadDay=new Date().toDateString();
function refreshIfStale(){
  if(document.hidden||feedLoading) return;
  if(new Date().toDateString()!==_loadDay){ _loadDay=new Date().toDateString(); loadCampaigns(true); }
}
document.addEventListener('visibilitychange',refreshIfStale);
window.addEventListener('focus',refreshIfStale);
setInterval(refreshIfStale, 60000);   // 1분마다 날짜 변경(자정) 감지 → 갱신
</script></body></html>"""