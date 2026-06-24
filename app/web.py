"""웹 UI: 텔레그램 로그인 + 키워드/지역 설정. 봇과 같은 SQLite DB를 공유."""
from __future__ import annotations
import hashlib
import hmac
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import db, config
from .matcher import matches
from .adapters.base import Campaign

WEB_REGIONS = [
    "서울", "경기", "인천", "강원", "충북", "충남", "대전", "세종",
    "전북", "전남", "광주", "경북", "경남", "대구", "울산", "부산", "제주", "전국",
]
WEB_CATEGORIES = ["맛집", "배송", "배달", "여가", "뷰티", "페이백", "기자단"]
WEB_CHANNELS = ["블로그", "릴스", "클립"]

app = FastAPI(title="체험단 알림 설정")
app.add_middleware(SessionMiddleware, secret_key=config.WEB_SECRET, max_age=60 * 60 * 24 * 30)


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


@app.get("/api/state")
async def state(request: Request):
    uid = _uid(request)
    if not uid:
        # 비로그인(게스트): 설정값은 비우고 logged_in=False 로 응답.
        return {
            "logged_in": False,
            "active": False,
            "name": "",
            "keywords": [], "regions": [], "categories": [], "channels": [],
            "max_competition": None, "max_dday": None,
            "all_regions": WEB_REGIONS,
            "all_categories": WEB_CATEGORIES,
            "all_channels": WEB_CHANNELS,
        }
    f = db.get_all_filters(uid)
    return {
        "logged_in": True,
        "active": db.is_active(uid),
        "name": request.session.get("name", ""),
        "keywords": f["keywords"],
        "regions": f["regions"],
        "categories": f["categories"],
        "channels": f["channels"],
        "max_competition": f["max_competition"],
        "max_dday": f["max_dday"],
        "all_regions": WEB_REGIONS,
        "all_categories": WEB_CATEGORIES,
        "all_channels": WEB_CHANNELS,
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


@app.post("/api/scalar")
async def set_scalar(request: Request):
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    key = body.get("key")
    if key not in ("max_competition", "max_dday"):
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
async def campaigns(request: Request):
    """필터에 맞는 최근 캠페인 전체(최신순) + NEW 여부.
    비로그인(게스트)이면 필터 없이 전체를 보여주고 NEW 표시는 하지 않는다."""
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
        md = _num("maxdday")
        f = {
            "keywords": _csv("kw"), "regions": _csv("region"),
            "categories": _csv("cat"), "channels": _csv("ch"),
            "max_competition": _num("maxcomp"),
            "max_dday": int(md) if md is not None else None,
        }
        seen_until, viewed = "9999-99-99", set()
    else:
        f = db.get_all_filters(uid)
        seen_until = db.get_seen_until(uid)
        viewed = db.viewed_set(uid)
    out, new_count = [], 0
    for r in db.list_recent(300):
        c = Campaign(
            site=r["site"], site_name="", cid=r["cid"], title=r["title"] or "",
            url=r["url"] or "", region=r["region"] or "", category=r["category"] or "",
            channel=r["channel"] or "", dday=r["dday"], applicants=r["applicants"],
            recruit=r["recruit"], competition=r["competition"],
        )
        if not matches(c, f["keywords"], f["regions"], f["categories"], f["channels"],
                       f["max_competition"], f["max_dday"]):
            continue
        is_new = ((r["first_seen"] or "") > seen_until) and ((r["site"], r["cid"]) not in viewed)
        if is_new:
            new_count += 1
        out.append({
            "site": r["site"], "cid": r["cid"], "title": c.title, "url": c.url,
            "region": c.region, "category": c.category, "channel": c.channel,
            "dday": c.dday, "competition": c.competition,
            "applicants": c.applicants, "recruit": c.recruit,
            "image": r["image"] if "image" in r.keys() else "", "is_new": is_new,
        })
    return {"campaigns": out, "new_count": new_count, "total": len(out)}


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


@app.post("/api/seen-all")
async def seen_all(request: Request):
    """모두 확인 → 현재까지 전부 NEW 해제."""
    uid = _uid(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.mark_all_seen(uid)
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # 로그인 여부와 상관없이 항상 메인(피드)을 보여준다.
    # 비로그인이면 상단 로그인 배너 + 피드만, 로그인이면 설정까지 노출.
    return HTMLResponse(_app_html())


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
<title>체험단 알림</title>
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
<title>내 알림 설정</title>
<style>
 :root{--blue:#2d6cdf}
 *{box-sizing:border-box}
 body{font-family:-apple-system,'Malgun Gothic',sans-serif;background:#f5f6f8;margin:0;color:#222}
 main{max-width:480px;margin:0 auto;padding:20px 16px 60px}
 header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
 h1{font-size:20px;margin:0}
 .sub{color:#666;font-size:13px;margin:0 0 20px}
 .card{background:#fff;border-radius:16px;padding:18px;box-shadow:0 1px 8px rgba(0,0,0,.05);margin-bottom:16px}
 .card h2{font-size:15px;margin:0 0 12px}
 .row{display:flex;gap:8px}
 input[type=text],input[type=number]{flex:1;padding:11px 12px;border:1px solid #d8dbe0;border-radius:10px;font-size:15px;width:100%}
 button{cursor:pointer;border:none;border-radius:10px;font-size:14px}
 .add{background:var(--blue);color:#fff;padding:0 16px;font-weight:600}
 .chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
 .chip{background:#eef2fb;color:#2d4373;border-radius:20px;padding:6px 12px;font-size:14px;display:flex;align-items:center;gap:6px}
 .chip b{cursor:pointer;color:#94a0c0;font-weight:700}
 .opts{display:flex;flex-wrap:wrap;gap:8px}
 .opt{padding:8px 14px;border:1px solid #d8dbe0;border-radius:20px;background:#fff;font-size:14px}
 .opt.on{background:var(--blue);color:#fff;border-color:var(--blue)}
 .muted{color:#888;font-size:13px;margin-top:8px}
 .top{display:flex;align-items:center;justify-content:space-between}
 .switch{position:relative;width:48px;height:28px}
 .switch input{display:none}
 .slider{position:absolute;inset:0;background:#ccc;border-radius:28px;transition:.2s}
 .slider:before{content:"";position:absolute;width:22px;height:22px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}
 .switch input:checked+.slider{background:#34c759}
 .switch input:checked+.slider:before{transform:translateX(20px)}
 a.logout{color:#888;font-size:13px;text-decoration:none}
 #feedwrap{max-height:600px;overflow-y:auto;margin-top:10px}
 #feed{display:grid;grid-template-columns:1fr 1fr;gap:10px}
 .item{position:relative;display:block;border:1px solid #eee;border-radius:12px;overflow:hidden;text-decoration:none;color:#222;background:#fff}
 .item:hover{box-shadow:0 2px 12px rgba(0,0,0,.09)}
 .item.viewed{opacity:.5}
 .thumb{width:100%;aspect-ratio:4/3;object-fit:cover;display:block;background:#eef0f3}
 .thumb.ph{display:flex;align-items:center;justify-content:center;color:#c2c8d0;font-size:24px}
 .body{padding:8px 9px 10px}
 .item .t{font-size:12.5px;font-weight:600;line-height:1.32;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:33px}
 .item .m{font-size:11px;color:#8a8a8a;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .pills{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
 .pill{font-size:10.5px;background:#f1f3f7;color:#566;border-radius:6px;padding:2px 6px}
 .pill.good{background:#e4f7e6;color:#1b7a2e;font-weight:700}
 .item.viewed .badge{display:none}
 .badge{position:absolute;top:6px;left:6px;background:#ff3b30;color:#fff;font-size:10px;font-weight:700;border-radius:7px;padding:2px 7px;z-index:1}
 .seenbtn{background:#eef2fb;color:#2d4373;padding:7px 12px;font-weight:600}
 .num{display:flex;align-items:center;gap:8px;margin-bottom:10px}
 .num label{width:64px;font-size:14px;color:#444}
 .preset{background:#fff4e6;color:#b25a00;border:1px solid #ffd8a8;padding:7px 12px;border-radius:20px;font-size:13px;font-weight:600}
 #loginbar{background:linear-gradient(135deg,#2d6cdf,#4a86ec);color:#fff;border-radius:16px;padding:18px;margin-bottom:16px;text-align:center;box-shadow:0 2px 12px rgba(45,108,223,.25)}
 #loginbar h2{font-size:16px;margin:0 0 4px}
 #loginbar p{color:#e7eefc;font-size:13px;margin:0 0 12px;line-height:1.5}
 #loginbar .wrap{display:flex;justify-content:center;min-height:40px}
</style></head><body><main>
<header><h1>🔔 체험단 알림</h1><a class=logout href="/logout" id=logoutlink style="display:none">로그아웃</a></header>
<p class=sub id=hello></p>

<div id=loginbar style="display:none">
  <h2>🔔 알림을 받아보세요</h2>
  <p>내 조건에 맞는 새 체험단이 뜨면<br>텔레그램으로 바로 알려드려요.</p>
  <div class=wrap>__LOGIN_WIDGET__</div>
</div>

<div id=notifcard style="display:none">
<div class=card><div class=top>
  <h2 style="margin:0">알림 받기</h2>
  <label class=switch><input type=checkbox id=active onchange="toggleActive()"><span class=slider></span></label>
</div><p class=muted>끄면 새 캠페인이 떠도 알림이 오지 않아요.</p></div>
</div>

<div class=card>
  <h2>⚡ 빠른 설정</h2>
  <div class=opts>
    <button class=preset onclick="preset('comp')">🎯 당첨확률 UP (경쟁률 ≤ 1)</button>
    <button class=preset onclick="preset('urgent')">⏰ 마감 임박 (D-3 이하)</button>
    <button class=preset onclick="preset('reset')">초기화</button>
  </div>
</div>

<div class=card>
  <h2>키워드</h2>
  <div class=row>
    <input type=text id=kw placeholder="예: 오마카세, 횡성" onkeydown="if(event.key==='Enter')addKw()">
    <button class=add onclick="addKw()">추가</button>
  </div>
  <div class=chips id=kwchips></div>
  <p class=muted>키워드는 하나라도 맞으면 알림 (예: 횡성 또는 평창).</p>
</div>

<div class=card>
  <h2>카테고리</h2>
  <div class=opts id=categories></div>
  <p class=muted>고른 카테고리만. 안 고르면 전체.</p>
</div>

<div class=card>
  <h2>채널 (콘텐츠 유형)</h2>
  <div class=opts id=channels></div>
  <p class=muted>블로그·릴스(=인스타)·클립. 디너의여왕은 블로그가 기본이에요.</p>
</div>

<div class=card>
  <h2>지역</h2>
  <div class=opts id=regions></div>
  <p class=muted>지역을 고르면 그 지역만. 키워드와 같이 쓰면 둘 다 맞아야 알림.</p>
</div>

<div class=card>
  <h2>상세 (당첨 확률)</h2>
  <div class=num><label>경쟁률 ≤</label>
    <input type=number step=0.1 min=0 id=maxcomp placeholder="예: 1" onchange="saveNum('max_competition','maxcomp')"></div>
  <div class=num><label>마감일 ≤</label>
    <input type=number min=0 id=maxdday placeholder="예: 3" onchange="saveNum('max_dday','maxdday')"></div>
  <p class=muted>경쟁률 = 신청자÷모집인원. 낮을수록 당첨 확률↑. 비워두면 제한 없음.</p>
</div>
<div class=card>
  <div class=top><h2 style="margin:0">📋 캠페인 <span id=cnt class=muted></span></h2>
    <button class=seenbtn onclick="seenAll()">모두 확인</button></div>
  <div id=feedwrap><div id=feed></div></div>
</div>
</main>
<script>
let S={}, guest=false;
async function load(){
  const r=await fetch('/api/state');
  S=await r.json();
  guest=!S.logged_in;
  document.getElementById('loginbar').style.display=guest?'':'none';
  document.getElementById('notifcard').style.display=guest?'none':'';
  document.getElementById('logoutlink').style.display=guest?'none':'';
  render();
  loadCampaigns();
}
function refreshLocal(){ render(); loadCampaigns(); }   // 게스트: 서버 저장 없이 화면만 갱신
function toggleArr(arr,v){const i=arr.indexOf(v); if(i>=0)arr.splice(i,1); else arr.push(v);}
async function loadCampaigns(){
  let url='/api/campaigns';
  if(guest){
    const p=new URLSearchParams();
    if(S.keywords.length)p.set('kw',S.keywords.join(','));
    if(S.regions.length)p.set('region',S.regions.join(','));
    if(S.categories.length)p.set('cat',S.categories.join(','));
    if(S.channels.length)p.set('ch',S.channels.join(','));
    if(S.max_competition!=null)p.set('maxcomp',S.max_competition);
    if(S.max_dday!=null)p.set('maxdday',S.max_dday);
    const qs=p.toString(); if(qs)url+='?'+qs;
  }
  const r=await fetch(url); if(r.status===401){return;}
  renderFeed(await r.json());
}
function esc(s){return (s||'').replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
function renderFeed(d){
  document.getElementById('cnt').textContent = d.new_count ? ('\u00b7 NEW '+d.new_count) : ('\u00b7 '+d.total+'\uac1c');
  const f=document.getElementById('feed'); f.innerHTML='';
  if(!d.campaigns.length){f.innerHTML='<p class=muted style="grid-column:1/-1">\uc870\uac74\uc5d0 \ub9de\ub294 \ucea0\ud398\uc778\uc774 \uc544\uc9c1 \uc5c6\uc5b4\uc694.</p>';return;}
  d.campaigns.forEach(c=>{
    const a=document.createElement('a'); a.className='item'+(c.is_new?'':' viewed');
    a.href=c.url; a.target='_blank'; a.rel='noopener';
    const meta=[c.region,c.category,c.channel].filter(Boolean).join(' \u00b7 ');
    let pills='';
    if(c.dday!=null) pills+='<span class=pill>D-'+c.dday+'</span>';
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
    a.addEventListener('click',()=>markView(c.site,c.cid,a));
    f.appendChild(a);
  });
}
function makePh(){const d=document.createElement('div');d.className='thumb ph';d.textContent='🍽️';return d;}
async function markView(site,cid,el){
  if(el.classList.contains('viewed'))return;
  el.classList.add('viewed'); const b=el.querySelector('.badge'); if(b)b.remove();
  const cnt=document.getElementById('cnt'); const m=cnt.textContent.match(/NEW (\\d+)/);
  if(m){const n=parseInt(m[1])-1; cnt.textContent=n>0?('· NEW '+n):'';}
  fetch('/api/view',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({site,cid})});
}
async function seenAll(){await fetch('/api/seen-all',{method:'POST'});await loadCampaigns();}
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
function render(){
  document.getElementById('hello').textContent = guest
    ? '키워드·지역으로 검색해 보세요. 알림으로 받고 싶으면 로그인하세요.'
    : (S.name?S.name+' ':'')+'설정은 자동 저장돼요.';
  document.getElementById('active').checked=!!S.active;
  chips('kwchips',S.keywords,delKw);
  opts('categories',S.all_categories,S.categories,toggleCategory);
  opts('channels',S.all_channels,S.channels,toggleChannel);
  opts('regions',S.all_regions,S.regions,toggleRegion);
  document.getElementById('maxcomp').value=(S.max_competition??'');
  document.getElementById('maxdday').value=(S.max_dday??'');
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
async function preset(kind){
  if(guest){
    if(kind==='comp')S.max_competition=1;
    else if(kind==='urgent')S.max_dday=3;
    else if(kind==='reset'){S.max_competition=null;S.max_dday=null;}
    refreshLocal();return;
  }
  if(kind==='comp'){await post('/api/scalar',{key:'max_competition',value:1});}
  else if(kind==='urgent'){await post('/api/scalar',{key:'max_dday',value:3});}
  else if(kind==='reset'){await post('/api/scalar',{key:'max_competition',value:null});
    await post('/api/scalar',{key:'max_dday',value:null});}
  await load();
}
load();
setInterval(loadCampaigns, 20000);  // 수집되는 대로 자동 갱신
</script></body></html>"""
