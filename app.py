"""
Scout: find unfinished UK sites (Base44, Lovable, Replit, Emergent, Bolt, Netlify, Vercel…), verify contacts, run WhatsApp + email outreach with follow-ups.

Start:   python app.py   (or double-click start-windows.bat / start-mac.command)
Opens:   http://localhost:8000
"""
import json, threading, datetime, uuid, urllib.request, re, time, os, sys, hmac, hashlib, secrets, subprocess, shutil
from pathlib import Path
from urllib.parse import urlparse
import urllib.parse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

import stopper, scout, dm_agent, outreach, autopilot, whatsapp_auto, discovery, companies, convo, platforms, quality, instagram

HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("SCOUT_DATA_DIR", HERE)); DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA = DATA_DIR / "scout_data.json"
if not DATA.exists() and DATA_DIR != HERE and (HERE / "scout_data.json").exists():
    shutil.copy(HERE / "scout_data.json", DATA)          # first run on a server: start with the leads you already have
PASSWORD = os.environ.get("SCOUT_PASSWORD", "")          # set this on a server: protects the dashboard
_secret_file = DATA_DIR / ".wa_secret"
WA_SECRET = os.environ.get("WA_SECRET") or (_secret_file.read_text().strip() if _secret_file.exists() else "")
if not WA_SECRET:
    WA_SECRET = secrets.token_hex(16); _secret_file.write_text(WA_SECRET)
WA_URL = os.environ.get("WA_URL", "")
WA = whatsapp_auto.WA(WA_URL or "http://127.0.0.1:3001", WA_SECRET)
LOCK = threading.RLock()
LOGBUF = []

def log(msg):
    line = f"{outreach.uk_now():%H:%M:%S} {msg}"          # UK time, same as the rest of the dashboard
    try: print(line, flush=True)
    except Exception:                      # Windows consoles can't always print emoji; never let logging break sending
        try: print(line.encode("ascii", "replace").decode(), flush=True)
        except Exception: pass
    LOGBUF.append(line); del LOGBUF[:-200]

def preset_queries(settings):
    doms = platforms.enabled(settings) or ["base44.app"]
    q = []
    for dom in doms:
        q += [f'site:{dom} "coming soon" "07" UK', f'site:{dom} cleaning "07" London']
    q += [f'site:{doms[0]} "lorem ipsum" UK', f'site:{doms[0]} handyman OR plumber OR electrician "07" UK', f'site:{doms[0]} barber OR salon OR lashes "07" UK']
    return q

# ---------------- storage ----------------
_CACHE = {"mtime": None, "d": None}
ACTIVE = {"scan_workers": 0}

def JOB(d, job_id):
    """A job's record, or a throwaway one if STOP already cleared it."""
    return d["jobs"].get(job_id) or {}

def job_stopped(job_id):
    """STOP pressed after this job started, or its own Stop button pressed?"""
    j = load()["jobs"].get(job_id)
    return not j or j.get("cancelled") or j.get("epoch", stopper.now()) != stopper.now()

def load():
    """Data lives in memory; the file is only re-read if something else changed it."""
    mt = DATA.stat().st_mtime_ns if DATA.exists() else None
    if _CACHE["d"] is not None and mt == _CACHE["mtime"]:
        return _CACHE["d"]
    d = json.loads(DATA.read_text()) if DATA.exists() else {}
    d = _migrate(d)
    _CACHE.update(mtime=mt, d=d)
    return d

def _migrate(d):
    d.setdefault("settings", {"name": "", "portfolio": "", "gemini_key": "", "serper_key": ""})
    d["settings"].setdefault("email", {})
    if d.get("email_speed_version", 0) < 1:
        email_settings = d["settings"]["email"]
        if email_settings.get("gap_min_s", 60) == 60 and email_settings.get("gap_max_s", 180) == 180:
            email_settings["gap_min_s"], email_settings["gap_max_s"] = 5, 15
        d["email_speed_version"] = 1
    d["settings"]["email"] = {**outreach.DEFAULT_EMAIL_SETTINGS, **d["settings"]["email"]}
    d["settings"]["autopilot"] = {**autopilot.DEFAULT_AUTOPILOT, **d["settings"].get("autopilot", {})}
    d["settings"]["whatsapp"] = {**whatsapp_auto.DEFAULT_WA, **d["settings"].get("whatsapp", {})}
    d["settings"]["whatsapp"]["daily_cap"] = min(int(d["settings"]["whatsapp"]["daily_cap"]), whatsapp_auto.HARD_MAX)
    for k in ("leads", "jobs"): d.setdefault(k, {})
    for k in ("suppression", "sent_log", "events"): d.setdefault(k, [])
    d.setdefault("sequences", json.loads(json.dumps(outreach.DEFAULT_SEQUENCES)))
    d["settings"]["voice"] = {**convo.DEFAULT_VOICE, **d["settings"].get("voice", {})}
    if d.get("opener_v") != 2:                      # one-time: gentler first message for leads not contacted yet
        for l in d["leads"].values():
            if l.get("source") != "company" and l.get("number") and not any(h["from"] == "me" for h in l.get("history", [])) and l.get("scan"):
                m, _ = dm_agent.opener({**l["scan"], "whatsapp": [w for w in l["scan"].get("whatsapp", []) if w["number"].startswith("+44")]})
                if m:
                    l["opener"] = re.sub(r"^Hi, is this .+?\?", f"Hi, is this {l['business']}?", m)
        d["opener_v"] = 2
    d.setdefault("sequence_sets", {}).setdefault("company", json.loads(json.dumps(companies.DEFAULT_COMPANY_SEQUENCES)))
    d["settings"].setdefault("company_price", 495)
    d["settings"]["co_auto"] = {**companies.DEFAULT_CO_AUTO, **d["settings"].get("co_auto", {})}
    d.setdefault("co_unresolved", [])
    for l in d["leads"].values():
        l.setdefault("seq", {}); l.setdefault("history", [])
        if "email" not in l: l["email"] = outreach.best_email(l)
        for h in l["history"]: h.setdefault("channel", "whatsapp")
    return d

def save(d):
    tmp = DATA.with_suffix(".tmp"); tmp.write_text(json.dumps(d, separators=(",", ":"))); tmp.replace(DATA)
    _CACHE.update(mtime=DATA.stat().st_mtime_ns, d=d)

def apply_settings(s):
    dm_agent.ME["name"] = s.get("name") or "YOUR_NAME"
    dm_agent.ME["portfolio"] = s.get("portfolio") or "YOUR_PORTFOLIO_LINK"
    if s.get("gemini_key"): os.environ["GEMINI_API_KEY"] = s["gemini_key"]
    else: os.environ.pop("GEMINI_API_KEY", None)

def now(): return datetime.datetime.now().isoformat(timespec="seconds")
def key_of(url): return platforms.lead_key(url)

def build_lead(raw, existing=None):
    uk_wa = [w for w in raw["whatsapp"] if w["number"].startswith("+44")]
    raw = {**raw, "whatsapp": uk_wa}
    msg, hook = dm_agent.opener(raw) if raw["is_uk"] and uk_wa else (None, None)
    lead = existing or {"status": "new", "history": [], "notes": "", "seq": {}}
    if raw.get("offline"): raw = {**raw, "is_uk": False, "title": ""}           # deleted site: keep for records, never contact
    lead.update({"key": key_of(raw["site"]), "scan": raw, "business": dm_agent.business_name(raw),
                 "opener": msg, "hook": hook, "entry_price": dm_agent.entry_price(raw),
                 "number": uk_wa[0]["number"] if uk_wa else None, "scanned_at": now()})
    if lead.get("custom_name"):
        lead["business"] = lead["custom_name"]
        if lead.get("opener"): lead["opener"] = re.sub(r"^Hi, is this .+?\?", f"Hi, is this {lead['custom_name']}?", lead["opener"])
    if not lead.get("email_locked"): lead["email"] = outreach.best_email(lead)
    return lead

def get_lead(d, k):
    l = d["leads"].get(k)
    if not l: raise HTTPException(404, "lead not found")
    return l

# ---------------- background: scans + outreach engine ----------------
def _scan_worker(job_id, q, pages, counter):
    """One browser, scanning sites from the shared queue. Several of these run side by side."""
    from playwright.sync_api import sync_playwright
    ACTIVE["scan_workers"] += 1
    try:
        _scan_worker_inner(job_id, q, pages, counter, sync_playwright)
    finally:
        ACTIVE["scan_workers"] -= 1

def _scan_worker_inner(job_id, q, pages, counter, sync_playwright):
    with sync_playwright() as p:
        br = p.chromium.launch()
        ctx = br.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")
        ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font") else route.continue_())
        pg = ctx.new_page()
        while True:
            if job_stopped(job_id): break   # you pressed STOP
            try: u = q.get_nowait()
            except Exception: break
            try:
                raw = scout.analyse(u, scout.scan_site(pg, u, pages))
                with LOCK:
                    d = load(); k = key_of(u); d["leads"][k] = build_lead(raw, d["leads"].get(k))
                    counter["done"] += 1; JOB(d, job_id).update(current=u, done=counter["done"]); save(d)
            except Exception as e:
                with LOCK:
                    d = load(); counter["done"] += 1
                    JOB(d, job_id).setdefault("errors", []).append(f"{u}: {str(e)[:100]}"); JOB(d, job_id)["done"] = counter["done"]; save(d)
                try: pg.close()
                except Exception: pass
                pg = ctx.new_page()
        br.close()

def run_scan(job_id, urls, pages):
    import queue as _queue
    workers = max(1, min(8, int(load()["settings"].get("scan_workers", 4))))
    q = _queue.Queue()
    for u in urls: q.put(u)
    counter = {"done": 0}
    threads = [threading.Thread(target=_scan_worker, args=(job_id, q, pages, counter), daemon=True) for _ in range(min(workers, len(urls)))]
    for t in threads: t.start()
    for t in threads: t.join()
    with LOCK:
        d = load(); j = JOB(d, job_id); stopped = bool(j.get("cancelled")) and counter["done"] < len(urls)
        j.update(done=counter["done"], current=None, finished=now(), stopped=stopped); save(d)
    log(f"scan stopped after {counter['done']} of {len(urls)} sites" if stopped else f"scan finished: {counter['done']} sites")

ENGINE_EVERY_S = 300
EMAIL_ENGINE_EVERY_S = 60
_wake = threading.Event()

def scan_sync(urls, pages=None):
    pages = pages or int(load()["settings"].get("scan_pages", 2))
    job = uuid.uuid4().hex[:8]
    with LOCK:
        d = load(); d["jobs"][job] = {"id": job, "total": len(urls), "done": 0, "current": None, "started": now(), "epoch": stopper.mine(), "auto": True}; save(d)
    run_scan(job, urls, pages)

def backup():
    bdir = DATA_DIR / "backups"; bdir.mkdir(exist_ok=True)
    f = bdir / f"scout_data-{datetime.date.today()}.json"
    if DATA.exists() and not f.exists():
        shutil.copy(DATA, f)
        for old in sorted(bdir.glob("scout_data-*.json"))[:-14]: old.unlink()

def engine_loop():
    """Email: inbox check + sending. Runs on its own so lead-hunting never delays messages."""
    while True:
        try: backup()
        except Exception as e: log(f"backup error: {e}")
        try:
            outreach.tick(load, save, LOCK, log)
        except Exception as e:
            log(f"engine error: {e}")
        _wake.wait(EMAIL_ENGINE_EVERY_S); _wake.clear()

_ap_wake = threading.Event()
_AP_LAST = {}
def autopilot_loop():
    """Finding + scanning new leads (slow) in its own thread."""
    time.sleep(20)
    while True:
        stopper.mark()                       # this pass ends as soon as you press STOP
        try:
            autopilot.run(load, save, LOCK, log, scan_sync, key_of)
        except Exception as e:
            log(f"autopilot error: {e}")
        try:
            co_autopilot_pass()
        except Exception as e:
            log(f"company autopilot error: {str(e)[:120]}")
        # behind target and still making progress? keep going instead of pausing 5 minutes
        try:
            d = load(); ap = autopilot.settings(d); t = d.get("autopilot_days", {}).get(autopilot.today(), {})
            behind = ap["enabled"] and t.get("enrolled", 0) < ap["daily_target"] and "budget used" not in t.get("notes", [])
            progressed = t.get("scans", 0) != _AP_LAST.get("scans") or t.get("enrolled", 0) != _AP_LAST.get("enrolled")
            _AP_LAST.update(scans=t.get("scans", 0), enrolled=t.get("enrolled", 0))
        except Exception:
            behind = progressed = False
        _ap_wake.wait(15 if (behind and progressed) else ENGINE_EVERY_S); _ap_wake.clear()

def wa_loop():
    while True:
        try:
            whatsapp_auto.tick(load, save, LOCK, log, WA)
        except Exception as e:
            log(f"whatsapp engine error: {e}")
        _wa_wake.wait(120); _wa_wake.clear()
_wa_wake = threading.Event()

def start_wa_service():
    """No Docker? Start wa-service next to Scout automatically (needs Node.js + npm install in wa-service)."""
    if WA_URL: return
    if load()["settings"]["whatsapp"].get("sender", "browser") != "service":
        log("WhatsApp: using your own WhatsApp Web through the Chrome extension"); return
    node = shutil.which("node"); svc = HERE / "wa-service"
    if not node or not (svc / "node_modules").exists():
        log("WhatsApp service not installed (run the launcher once, or 'npm install' in wa-service)"); return
    env = {**os.environ, "PORT": "3001", "SCOUT_URL": f"http://127.0.0.1:{os.environ.get('PORT', '8000')}",
           "WA_SECRET": WA_SECRET, "SESSION_DIR": str(DATA_DIR / "wa-session")}
    logf = open(DATA_DIR / "wa-service.log", "a")
    subprocess.Popen([node, "index.js"], cwd=svc, env=env, stdout=logf, stderr=logf)
    log("WhatsApp service started")

# ---------------- API ----------------
app = FastAPI(title="Scout")

def _token(): return hmac.new(PASSWORD.encode(), b"scout-login", hashlib.sha256).hexdigest()

@app.middleware("http")
async def auth(request: Request, call_next):
    p = request.url.path
    if not PASSWORD or p in ("/login", "/health", "/api/wa/incoming", "/api/instagram/webhook") or p.startswith("/api/waweb/") or request.cookies.get("scout_auth") == _token():
        return await call_next(request)
    if p.startswith("/api/"): return JSONResponse({"detail": "login required"}, status_code=401)
    return RedirectResponse("/login")

LOGIN_HTML = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Scout login</title>
<style>body{font-family:system-ui;background:#0b0f17;color:#e6e9ef;display:grid;place-items:center;height:100vh;margin:0}
form{background:#121826;padding:28px;border-radius:14px;width:300px;border:1px solid #232d40}input,button{width:100%;padding:11px;border-radius:8px;font:inherit;box-sizing:border-box}
input{background:#0b0f17;border:1px solid #232d40;color:inherit;margin:12px 0}button{background:#22c55e;border:0;color:#fff;font-weight:600;cursor:pointer}p{color:#f87171;font-size:13px}</style>
<form method=post><b style="font-size:18px">Scout</b><input type=password name=password placeholder=Password autofocus><button>Log in</button>%s</form>"""

@app.get("/logout")
def logout():
    r = RedirectResponse("/login"); r.delete_cookie("scout_auth"); return r

@app.get("/login", response_class=HTMLResponse)
def login_page(): return LOGIN_HTML.replace("%s", "")

@app.get("/health")
def health(): return {"ok": True}

@app.post("/login")
async def login(request: Request):
    form = (await request.body()).decode()
    pw = urllib.parse.unquote_plus(form.split("password=", 1)[-1]) if "password=" in form else ""
    if PASSWORD and hmac.compare_digest(pw, PASSWORD):
        r = RedirectResponse("/", status_code=303)
        r.set_cookie("scout_auth", _token(), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
        return r
    time.sleep(1); return HTMLResponse(LOGIN_HTML.replace("%s", "<p>Wrong password</p>"), status_code=401)

# ================= WhatsApp through your own WhatsApp Web (Chrome extension) =================
import random as _random, io, zipfile
WAWEB = {"pending": None, "next_allowed": 0.0, "last_seen": 0.0}

def _waweb_auth(request: Request):
    if request.headers.get("x-scout-token") != WA_SECRET:
        raise HTTPException(401, "Wrong connection code. Copy it again from Scout → Settings → WhatsApp.")

def record_wa_reply(d, l, text, via):
    l["history"].append({"from": "them", "channel": "whatsapp", "text": text, "at": now()})
    l["unread"] = True; outreach.stop_all(l, "replied")
    if outreach.UNSUB_RE.search(text) and len(text) < 300:
        l["status"] = "do_not_contact"
        for c in (l.get("email"), l.get("number")):
            if c and c not in d["suppression"]: d["suppression"].append(c)
    elif l["status"] in ("new", "contacted", "later"): l["status"] = "talking"
    d["events"].append({"type": "reply", "key": l["key"], "at": outreach.iso(outreach.uk_now()), "channel": "whatsapp"})
    log(f"💬 WhatsApp reply from {l['business']} ({via})")
    queue_autodraft(l["key"])

@app.get("/api/waweb/next")
def waweb_next(request: Request, test: int = 0):
    """The extension asks: anything to send right now? Scout decides, so all limits stay in one place."""
    _waweb_auth(request); WAWEB["last_seen"] = time.time()
    d = load(); ws = d["settings"]["whatsapp"]; P = whatsapp_auto.PROGRESS
    leads = [l for l in d["leads"].values() if l.get("number")]
    out = {"numbers": [l["number"].lstrip("+") for l in leads], "names": [l["business"].strip().lower() for l in leads], "job": None}
    def status(msg):
        out["status"] = msg
        if P["active"] and not WAWEB["pending"] and msg != "waiting":
            P.update(active=False, current=None, next_at=None, queue=[], finished=outreach.iso(outreach.uk_now()))
        return out
    if test: return status("connected")
    if d["settings"].get("engine_paused"): out["status"] = "connected · Scout is stopped"; return out
    if ws.get("sender", "browser") != "browser": return status("Scout is set to the built-in WhatsApp service")
    outbox = d.get("wa_outbox", [])
    if outbox and not (WAWEB["pending"] and time.time() - WAWEB["pending"]["t"] < 180) and time.time() >= WAWEB.get("reply_after", 0):
        item = outbox[0]
        with LOCK:
            dd = load(); dd["wa_outbox"] = [x for x in dd.get("wa_outbox", []) if x["id"] != item["id"]]; save(dd)
        job = {**item, "kind": "reply", "step": 0}
        WAWEB["pending"] = {**job, "t": time.time()}
        out["job"] = job; out["status"] = "sending your approved reply"
        return out
    if not ws["auto"]: return status("connected · auto-send is off in Scout")
    if not whatsapp_auto.in_window(ws): return status("connected · outside sending hours")
    sent, cap = whatsapp_auto.sent_today(d), whatsapp_auto.effective_cap(d, ws)
    if sent >= cap: return status(f"connected · today's limit reached ({sent}/{cap})")
    pend = WAWEB["pending"]
    if pend and time.time() - pend["t"] < 180: out["status"] = "waiting"; return out
    todo = whatsapp_auto.due(d)
    if not todo: return status("connected · nothing due")
    if time.time() < WAWEB["next_allowed"]:
        wait = int(WAWEB["next_allowed"] - time.time())
        out["status"] = f"next message in {wait // 60}:{wait % 60:02d}"; return out
    k = todo[0]; l = d["leads"][k]; s = l["seq"]["whatsapp"]
    text = outreach.render(outreach.seqs_for(d, l)["whatsapp"][s["step"]]["body"], l, d["settings"])
    job = {"id": uuid.uuid4().hex[:10], "key": k, "number": l["number"].lstrip("+"), "text": text, "business": l["business"], "step": s["step"]}
    WAWEB["pending"] = {**job, "t": time.time()}
    if not P["active"]:
        P.update(active=True, total=max(1, min(len(todo), cap - sent)), done=0, failed=0, results=[], stopped=None,
                 started=outreach.iso(outreach.uk_now()), finished=None)
    P.update(current={"business": l["business"], "to": l["number"], "step": s["step"] + 1}, next_at=None,
             queue=[d["leads"][x]["business"] for x in todo[1:P["total"] - P["done"] - P["failed"]]])
    out["job"] = job; out["status"] = "sending"
    return out

@app.post("/api/waweb/result")
async def waweb_result(request: Request):
    _waweb_auth(request); m = await request.json(); pend = WAWEB["pending"]; P = whatsapp_auto.PROGRESS
    if not pend or pend["id"] != m.get("id"): return {"ok": False, "detail": "unknown job"}
    WAWEB["pending"] = None
    if pend.get("kind") == "reply":                     # an approved reply, not a sequence step
        WAWEB["reply_after"] = time.time() + 20
        with LOCK:
            d = load(); l = d["leads"].get(pend["key"])
            if l:
                if m.get("ok"):
                    l["history"].append({"from": "me", "channel": "whatsapp", "text": pend["text"], "at": now(), "approved": True})
                    l["unread"] = False
                else:
                    l["pending_draft"] = {**(l.get("pending_draft") or {}), "text": pend["text"], "channel": "whatsapp",
                                          "error": f"sending failed ({m.get('error')}), try again"}
                save(d)
        log(f"📱 approved reply to {pend['business']}: " + ("sent ✓" if m.get("ok") else f"failed ({m.get('error')})"))
        return {"ok": True}
    k, ok, err = pend["key"], bool(m.get("ok")), m.get("error")
    with LOCK:
        d = load(); l = d["leads"].get(k); ws = d["settings"]["whatsapp"]
        if l and ok:
            l["history"].append({"from": "me", "channel": "whatsapp", "text": pend["text"], "auto": True, "step": pend["step"], "at": now()})
            if l["status"] == "new": l["status"] = "contacted"
            outreach.advance(l, "whatsapp", outreach.seqs_for(d, l))
            d.setdefault("wa_log", []).append({"at": outreach.iso(outreach.uk_now()), "key": k, "step": pend["step"]})
            d["wa_log"] = d["wa_log"][-5000:]; d.setdefault("engine", {})["wa_error"] = None
        elif l and err == "not_on_whatsapp":
            l["wa_status"] = "not_on_whatsapp"
            l["seq"]["whatsapp"].update(state="stopped", stopped_reason="number not on WhatsApp", next_at=None)
        else:
            d.setdefault("engine", {})["wa_error"] = f"WhatsApp Web: {err}"
        save(d)
    gap = _random.uniform(float(ws["gap_min_s"]), float(ws["gap_max_s"])) if ok else 60
    WAWEB["next_allowed"] = time.time() + gap
    P["done" if ok else "failed"] += 1
    P["results"].insert(0, {"business": pend["business"], "to": "+" + pend["number"], "step": pend["step"] + 1, "ok": ok,
                            "at": outreach.iso(outreach.uk_now()), "note": "" if ok else ("not on WhatsApp" if err == "not_on_whatsapp" else str(err))})
    del P["results"][20:]
    P.update(current=None, next_at=outreach.iso(outreach.uk_now() + datetime.timedelta(seconds=gap)))
    log(f"📱 WhatsApp to {pend['business']}: " + ("sent ✓" if ok else f"failed ({err})"))
    return {"ok": True}

@app.post("/api/waweb/incoming")
async def waweb_incoming(request: Request):
    """A reply spotted in your WhatsApp Web chat list (only chats with your leads)."""
    _waweb_auth(request); m = await request.json()
    num = "+" + re.sub(r"\D", "", str(m.get("number", ""))); title = str(m.get("title", "")).strip().lower(); text = str(m.get("text", ""))[:2000]
    with LOCK:
        d = load()
        l = next((x for x in d["leads"].values() if x.get("number") and (x["number"] == num or x["business"].strip().lower() == title)), None)
        if not l or not text: return {"matched": False}
        key = f"web|{l['key']}|{text}"; seen = d.setdefault("wa_seen", [])
        if key in seen or any(h["from"] == "them" and h["text"] == text for h in l["history"]): return {"matched": True, "dup": True, "business": l["business"]}
        seen.append(key); d["wa_seen"] = seen[-3000:]
        record_wa_reply(d, l, text, "WhatsApp Web"); save(d)
    return {"matched": True, "business": l["business"]}

@app.get("/api/wa/browser-info")
def waweb_info():
    return {"token": WA_SECRET, "connected": time.time() - WAWEB["last_seen"] < 90, "last_seen": WAWEB["last_seen"]}

@app.get("/whatsapp-extension.zip")
def extension_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in (HERE / "whatsapp-extension").iterdir():
            z.write(f, f"scout-whatsapp-extension/{f.name}")
    from fastapi.responses import Response
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": "attachment; filename=scout-whatsapp-extension.zip"})

@app.get("/api/progress")
def progress():
    """Small, fast status for the live 'Sending now' panel (polled every few seconds)."""
    d = load(); es = d["settings"]["email"]; ws = d["settings"]["whatsapp"]
    due_e = outreach.due_emails(d); due_w = whatsapp_auto.due(d)
    cap_e = int(es["daily_cap"]); sent_e = outreach.sent_today(d)
    if es["mode"] != "live": why = "Email sending is paused. Switch it to LIVE above."
    elif not outreach.email_ready(es): why = "Email isn't set up yet (Settings)."
    elif not outreach.in_window(es): why = f"Outside sending hours ({es['start_hour']}:00–{es['end_hour']}:00 UK" + (", weekdays only)" if es.get("weekdays_only") else ")")
    elif sent_e >= cap_e: why = f"Today's limit reached ({sent_e}/{cap_e}). The rest go out tomorrow."
    else: why = "Starting within a minute (or press Run now)."
    return {"email": {**outreach.PROGRESS, "due": len(due_e), "sent_today": sent_e, "cap": cap_e, "waiting_reason": why if due_e else "",
                      "due_list": [{"business": d["leads"][k]["business"], "to": d["leads"][k].get("email")} for k in due_e[:15]]},
            "wa": {**whatsapp_auto.PROGRESS, "due": len(due_w), "auto": ws["auto"], "sent_today": whatsapp_auto.sent_today(d),
                   "cap": whatsapp_auto.effective_cap(d, ws)},
            "today_emails": [{"business": d["leads"].get(x["key"], {}).get("business", x["key"]), "to": x.get("to"),
                              "step": x.get("step", 0) + 1 if x.get("step") is not None else None, "manual": x.get("manual", False), "at": x["at"]}
                             for x in reversed(d["sent_log"]) if x["at"][:10] == outreach.uk_now().date().isoformat()][:60],
            "today_wa": [{"business": d["leads"].get(x["key"], {}).get("business", x["key"]), "step": x.get("step", 0) + 1, "at": x["at"]}
                         for x in reversed(d.get("wa_log", [])) if x["at"][:10] == outreach.uk_now().date().isoformat()][:60],
            "log": LOGBUF[-15:][::-1], "uk_time": outreach.uk_now().strftime("%H:%M:%S")}

@app.get("/api/wa/status")
def wa_status():
    st = WA.status(); st.pop("lastError", None) if st.get("state") == "ready" else None
    return st

@app.post("/api/wa/logout")
def wa_logout(): return WA.logout()

@app.post("/api/wa/incoming")
async def wa_incoming(request: Request):
    if request.headers.get("x-scout-secret") != WA_SECRET: raise HTTPException(401)
    m = await request.json(); num = "+" + re.sub(r"\D", "", str(m.get("from", ""))); text = str(m.get("text", ""))[:4000]
    with LOCK:
        d = load(); seen = d.setdefault("wa_seen", [])
        if m.get("id") and m["id"] in seen: return {"ok": True, "dup": True}
        if m.get("id"): seen.append(m["id"]); d["wa_seen"] = seen[-3000:]
        l = next((x for x in d["leads"].values() if x.get("number") == num), None)
        if not l: save(d); return {"ok": True, "matched": False}
        l["history"].append({"from": "them", "channel": "whatsapp", "text": text, "at": now()})
        l["unread"] = True; outreach.stop_all(l, "replied")
        if outreach.UNSUB_RE.search(text) and len(text) < 300:
            l["status"] = "do_not_contact"
            for c in (l.get("email"), l.get("number")):
                if c and c not in d["suppression"]: d["suppression"].append(c)
        elif l["status"] in ("new", "contacted", "later"): l["status"] = "talking"
        d["events"].append({"type": "reply", "key": l["key"], "at": outreach.iso(outreach.uk_now()), "channel": "whatsapp"})
        save(d)
    log(f"whatsapp reply: {l['key']}"); queue_autodraft(l["key"])
    return {"ok": True, "matched": True}

class InstagramReplyReq(BaseModel):
    text: str

@app.get("/api/instagram/webhook")
def instagram_webhook_verify(request: Request):
    cfg = instagram.settings()
    if not cfg["verify_token"]: raise HTTPException(503, "Instagram webhook verification is not configured")
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")
    if not instagram.verify_challenge(mode, token, cfg["verify_token"]): raise HTTPException(403, "Webhook verification failed")
    return PlainTextResponse(challenge)

@app.post("/api/instagram/webhook")
async def instagram_webhook(request: Request):
    cfg = instagram.settings()
    if not cfg["app_secret"] or not cfg["account_id"].isdigit() or len(cfg["account_id"]) > 64:
        raise HTTPException(503, "Instagram webhook is not configured")
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > 1_000_000:
        raise HTTPException(413, "Webhook payload is too large")
    raw = await request.body()
    if len(raw) > 1_000_000: raise HTTPException(413, "Webhook payload is too large")
    if not instagram.verify_signature(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "Invalid Instagram webhook signature")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid webhook JSON")
    if not isinstance(payload, dict): raise HTTPException(400, "Invalid webhook payload")

    incoming = instagram.parse_events(payload, cfg["account_id"])
    accepted = 0
    with LOCK:
        d = load()
        conversations = d.setdefault("instagram", {}).setdefault("conversations", {})
        seen = d.setdefault("instagram", {}).setdefault("seen", [])
        for item in incoming:
            if item["id"] and item["id"] in seen: continue
            sender_id = item["sender_id"]
            convo = conversations.setdefault(sender_id, {
                "sender_id": sender_id, "username": "", "messages": [], "unread": False,
                "last_inbound_at": None, "updated_at": None,
            })
            if item["username"]: convo["username"] = item["username"]
            convo["messages"].append({"id": item["id"], "from": "them", "text": item["text"], "at": item["at"]})
            convo["messages"].sort(key=lambda message: message.get("at") or "")
            convo["last_inbound_at"] = max(convo.get("last_inbound_at") or "", item["at"])
            convo["updated_at"] = max(convo.get("updated_at") or "", item["at"])
            convo["unread"] = True
            if item["id"]: seen.append(item["id"])
            accepted += 1
        d["instagram"]["seen"] = seen[-10000:]
        save(d)
    return {"ok": True, "accepted": accepted}

@app.get("/api/instagram/status")
def instagram_status(request: Request):
    return {**instagram.status(), "webhook_url": str(request.base_url).rstrip("/") + "/api/instagram/webhook"}

@app.get("/api/instagram/inbox")
def instagram_inbox():
    d = load()
    conversations = list(d.get("instagram", {}).get("conversations", {}).values())
    conversations.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    return {"conversations": conversations}

@app.post("/api/instagram/conversations/{sender_id}/read")
def instagram_mark_read(sender_id: str):
    with LOCK:
        d = load(); convo = d.get("instagram", {}).get("conversations", {}).get(sender_id)
        if not convo: raise HTTPException(404, "Instagram conversation not found")
        convo["unread"] = False; save(d)
    return {"ok": True}

@app.post("/api/instagram/conversations/{sender_id}/reply")
def instagram_reply(sender_id: str, message: InstagramReplyReq):
    text = message.text.strip()
    if not text or len(text) > 1000: raise HTTPException(400, "Reply must contain 1-1000 characters")
    if not instagram.status()["configured"]: raise HTTPException(503, "Instagram Messaging API is not configured")
    d = load(); convo = d.get("instagram", {}).get("conversations", {}).get(sender_id)
    if not convo: raise HTTPException(404, "Instagram conversation not found")
    if not instagram.can_reply(convo): raise HTTPException(403, "The Instagram 24-hour messaging window has expired")
    try:
        result = instagram.send_message(sender_id, text)
    except ValueError as error:
        raise HTTPException(400, str(error))
    except RuntimeError as error:
        raise HTTPException(502, str(error))
    with LOCK:
        d = load(); convo = d.get("instagram", {}).get("conversations", {}).get(sender_id)
        if not convo: raise HTTPException(404, "Instagram conversation not found")
        sent_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        convo["messages"].append({"id": str(result.get("message_id") or ""), "from": "me", "text": text, "at": sent_at})
        convo["updated_at"] = sent_at
        convo["unread"] = False
        save(d)
    return {"ok": True, "message_id": result.get("message_id")}

class ScanReq(BaseModel):
    urls: list[str]; pages: int = 3
class SearchReq(BaseModel):
    query: str; pages: int = 1
class MsgReq(BaseModel):
    text: str; channel: str = "whatsapp"; subject: str | None = None
class LeadPatch(BaseModel):
    status: str | None = None; notes: str | None = None; custom_name: str | None = None; email: str | None = None
class SeqReq(BaseModel):
    channel: str; action: str          # start | pause | resume | stop
class EnrollReq(BaseModel):
    channels: list[str]; keys: list[str] | None = None

@app.get("/", response_class=HTMLResponse)
def index(): return (HERE / "index.html").read_text(encoding="utf-8")

@app.get("/api/state")
def state():
    d = load()
    s = json.loads(json.dumps(d["settings"]))
    for k in ("gemini_key", "serper_key", "firecrawl_key"): s[k] = bool(s.get(k))
    s["searxng_url"] = d["settings"].get("searxng_url") or ""
    s["searxng_env"] = bool(os.environ.get("SEARXNG_URL"))
    s["email"]["smtp_pass"] = bool(s["email"].get("smtp_pass"))
    es = d["settings"]["email"]
    eng = {**d.get("engine", {}), "paused": bool(d["settings"].get("engine_paused")), "stopping": STOPPING["active"], "ready": outreach.email_ready(es), "mode": es["mode"], "in_window": outreach.in_window(es),
           "sent_today": outreach.sent_today(d), "cap": es["daily_cap"], "due_now": len(outreach.due_emails(d)),
           "uk_time": outreach.uk_now().strftime("%a %H:%M")}
    ap_today = d.get("autopilot_days", {}).get(autopilot.today(), {"enrolled": 0, "searches": 0, "scans": 0, "notes": []})
    ws = d["settings"]["whatsapp"]
    eng["wa"] = {"sent_today": whatsapp_auto.sent_today(d), "cap": whatsapp_auto.effective_cap(d, ws),
                 "in_window": whatsapp_auto.in_window(ws), "due_now": len(whatsapp_auto.due(d)), "error": d.get("engine", {}).get("wa_error")}
    eng["autopilot"] = {**ap_today, "pool": len(autopilot.eligible(d, d["settings"]["autopilot"])),
                        "history": {k: v["enrolled"] for k, v in sorted(d.get("autopilot_days", {}).items())[-14:]}}
    qs = quality.score_all(d)
    for k, l in d["leads"].items():
        l["can_email"] = outreach.can_email(l, d); l["can_wa"] = outreach.can_whatsapp(l, d); l["quality"] = qs.get(k)
    eng["quality_report"] = quality.report(d)
    today_ = autopilot.today()
    fresh = [l for l in d["leads"].values() if l.get("source") != "company" and (l.get("scanned_at") or "")[:10] == today_]
    eng["funnel"] = {"scanned": len(fresh), "uk": sum(1 for l in fresh if l["scan"].get("is_uk")),
                     "contactable": sum(1 for l in fresh if l["scan"].get("is_uk") and (outreach.can_email(l, d) or outreach.can_whatsapp(l, d))),
                     "skipped_competitor": sum(1 for l in fresh if l["scan"].get("competitor")), "offline": sum(1 for l in fresh if l["scan"].get("offline")),
                     "started": ap_today.get("enrolled", 0), "workers": int(d["settings"].get("scan_workers", 4)), "pages": int(d["settings"].get("scan_pages", 2))}
    eng["co_today"] = d.get("co_days", {}).get(autopilot.today(), {"searches": 0, "found": 0, "started": 0})
    eng["discovery"] = {"pool": discovery.pool_stats(d), "sources": d.get("discovery", {}), "refreshing": _pool_busy.is_set(),
                        "providers": [n for n, _ in discovery.keyword_providers(d["settings"])]}
    return {"settings": s, "leads": list(d["leads"].values()), "jobs": d["jobs"], "presets": preset_queries(d["settings"]), "platforms": [{"domain": dm, "label": lb, "on": dm in platforms.enabled(d["settings"])} for dm, lb, _ in platforms.PLATFORMS],
            "instagram": instagram.status(),
            "sequences": d["sequences"], "sequence_sets": d["sequence_sets"], "co_unresolved": d["co_unresolved"][-200:],
            "sectors": [{"label": a, "link": companies.europages_link(b)} for a, b in companies.SECTORS], "engine": eng, "suppression": d["suppression"], "log": LOGBUF[-40:]}

@app.post("/api/settings")
def settings(body: dict):
    with LOCK:
        d = load()
        for k in ("name", "portfolio", "gemini_key", "serper_key", "firecrawl_key", "searxng_url"):
            if body.get(k) not in (None, ""): d["settings"][k] = str(body[k]).strip()
        if k := body.get("clear"): d["settings"][k] = ""
        for k, v in (body.get("email") or {}).items():
            if k not in outreach.DEFAULT_EMAIL_SETTINGS or v in (None, ""): continue
            cur = outreach.DEFAULT_EMAIL_SETTINGS[k]
            d["settings"]["email"][k] = (v if isinstance(cur, bool) else int(v) if isinstance(cur, int) else str(v).strip())
        if body.get("scan_workers"): d["settings"]["scan_workers"] = max(1, min(8, int(body["scan_workers"])))
        if body.get("scan_pages"): d["settings"]["scan_pages"] = max(1, min(4, int(body["scan_pages"])))
        for k, v in (body.get("co_auto") or {}).items():
            if k not in companies.DEFAULT_CO_AUTO or v is None: continue
            cur = companies.DEFAULT_CO_AUTO[k]
            if isinstance(cur, bool): v = bool(v)
            elif isinstance(cur, int): v = max(0, int(v))
            elif isinstance(cur, list): v = [x.strip() for x in (v if isinstance(v, list) else str(v).split(",")) if x.strip()]
            else: v = str(v)
            d["settings"]["co_auto"][k] = v
        if isinstance(body.get("platforms"), dict):
            d["settings"]["platforms"] = {dm: bool(body["platforms"].get(dm, on)) for dm, _, on in platforms.PLATFORMS}
        for k, v in (body.get("voice") or {}).items():
            if k not in convo.DEFAULT_VOICE or v is None: continue
            d["settings"]["voice"][k] = int(v) if k in ("directness", "warmth") else str(v)[:4000]
        for k, v in (body.get("whatsapp") or {}).items():
            if k not in whatsapp_auto.DEFAULT_WA or v is None or v == "": continue
            cur = whatsapp_auto.DEFAULT_WA[k]
            v = bool(v) if isinstance(cur, bool) else (str(v) if isinstance(cur, str) else max(0, int(v)))
            if k == "daily_cap": v = min(v, whatsapp_auto.HARD_MAX)
            d["settings"]["whatsapp"][k] = v
        for k, v in (body.get("autopilot") or {}).items():
            if k not in autopilot.DEFAULT_AUTOPILOT or v is None: continue
            cur = autopilot.DEFAULT_AUTOPILOT[k]
            if isinstance(cur, bool): v = bool(v)
            elif isinstance(cur, int): v = max(0, int(v))
            elif isinstance(cur, list): v = [x.strip() for x in (v if isinstance(v, list) else str(v).split(",")) if x.strip()]
            d["settings"]["autopilot"][k] = v
        save(d)
    apply_settings(d["settings"]); _wake.set(); _wa_wake.set(); _ap_wake.set(); return {"ok": True}

@app.post("/api/email/test")
def email_test(body: dict):
    """Send step N of the sequence for a lead (or a sample) to YOUR OWN address. Changes nothing."""
    d = load(); es = d["settings"]["email"]
    if not outreach.email_ready(es): raise HTTPException(400, "Fill in your email address, SMTP server and app password first.")
    l = d["leads"].get(body.get("key") or "") or next((x for x in d["leads"].values() if x.get("email") and x["scan"]["is_uk"]), None)
    if not l: raise HTTPException(400, "No lead with an email to preview")
    step = int(body.get("step", 0))
    subj, text = outreach.render_email_step(l, d["settings"], outreach.seqs_for(d, l), step)
    try:
        outreach.smtp_send(es, es["from_email"], f"[TEST → {l.get('email')}] {subj}", text)
    except Exception as e:
        raise HTTPException(400, f"Sending failed: {e}")
    return {"ok": True, "to": es["from_email"], "subject": subj}

@app.post("/api/email/check")
def email_check():
    d = load(); es = d["settings"]["email"]
    if not outreach.email_ready(es): raise HTTPException(400, "Email not set up")
    try:
        msgs = outreach.fetch_inbox(es)
    except Exception as e:
        raise HTTPException(400, f"Inbox check failed: {e}")
    with LOCK:
        d = load(); ev = outreach.process_messages(d, msgs)
        for e in ev:
            if e["type"] == "reply": queue_autodraft(e["key"])
        d["events"].extend(ev); d.setdefault("engine", {}).update(last_inbox=outreach.iso(outreach.uk_now()), inbox_error=None); save(d)
    return {"checked": len(msgs), "events": ev}

STOPPING = {"active": False, "since": None}

def _finish_stop():
    """Wait (max 30s) until scans and sending have really ended, then clear the stopped job cards."""
    t0 = time.time()
    while time.time() - t0 < 30 and (ACTIVE["scan_workers"] > 0 or outreach.PROGRESS.get("active")):
        time.sleep(0.5)
    with LOCK:
        d = load()
        for jid in [k for k, j in d["jobs"].items() if j.get("cancelled") or j.get("finished")]: d["jobs"].pop(jid, None)
        save(d)
    STOPPING.update(active=False)
    log("■ Scout fully stopped. Everything is off; switch on only what you want")

def stop_everything(reason="by you"):
    """STOP: end everything now, switch every automation OFF, and reset searches and queues."""
    stopper.stop()                                        # every running loop quits at its next check (within ~1s when waiting)
    with LOCK:
        d = load(); s = d["settings"]
        s["engine_paused"] = False                        # (old Pause switch, no longer used)
        s["autopilot"]["enabled"] = False
        s["co_auto"]["enabled"] = False
        s["whatsapp"]["auto"] = False
        s["email"]["mode"] = "paused"                     # email sending OFF
        for j in d["jobs"].values():
            if not j.get("finished"): j.update(cancelled=True, finished=now(), current=None, stopped=True)
        # approved WhatsApp replies that hadn't gone yet go back to "Waiting for your OK", nothing is lost
        for item in d.get("wa_outbox", []):
            l = d["leads"].get(item["key"])
            if l and not l.get("pending_draft"):
                l["pending_draft"] = {"text": item["text"], "channel": "whatsapp", "source": "held back by STOP", "at": now()}
        d["wa_outbox"] = []
        # reset searches: forget which searches were used; today's search budget starts again
        d["queries_used"] = {}; d["co_queries_used"] = {}
        t = d.get("autopilot_days", {}).get(autopilot.today())
        if t: t.update(searches=0, notes=[])
        c = d.get("co_days", {}).get(autopilot.today())
        if c: c["searches"] = 0
        save(d)
    WAWEB["pending"] = None; WAWEB["next_allowed"] = 0.0
    outreach.PROGRESS.update(stopped="stopped by you", queue=[], next_at=None)
    whatsapp_auto.PROGRESS.update(active=False, current=None, next_at=None, queue=[], stopped="stopped by you", finished=outreach.iso(outreach.uk_now()))
    STOPPING.update(active=True, since=now())
    _wake.set(); _wa_wake.set(); _ap_wake.set()            # wake sleeping loops so they see everything is off
    log(f"■ STOP pressed ({reason}): sending, Autopilot, Company Autopilot, searches and scans stopped and switched off")
    threading.Thread(target=_finish_stop, daemon=True).start()

@app.post("/api/engine/stop")
def engine_stop():
    stop_everything()
    return {"ok": True}

@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    with LOCK:
        d = load(); j = d["jobs"].get(job_id)
        if not j: raise HTTPException(404, "job not found")
        j["cancelled"] = True; save(d)
    log("■ scan stopped by you (it finishes the site it's on)")
    return {"ok": True}

@app.post("/api/engine/run")
def engine_run(): _wake.set(); return {"ok": True}

@app.post("/api/sequences")
def save_sequences(body: dict, set: str = "main"):
    for ch in ("email", "whatsapp"):
        steps = body.get(ch)
        if not steps or not isinstance(steps, list): raise HTTPException(400, f"{ch}: at least one step needed")
        for s in steps:
            s["day"] = int(s.get("day", 0))
            try: s["body"].format_map(outreach.defaultdict(str))
            except Exception as e: raise HTTPException(400, f"Template error in {ch}: {e}")
        if [s["day"] for s in steps] != sorted(s["day"] for s in steps): raise HTTPException(400, f"{ch}: days must go up")
    with LOCK:
        d = load(); new = {"email": body["email"], "whatsapp": body["whatsapp"]}
        if set == "main": d["sequences"] = new
        else: d["sequence_sets"][set] = new
        save(d)
    return {"ok": True}

@app.post("/api/sequences/reset")
def reset_sequences(set: str = "main"):
    with LOCK:
        d = load()
        if set == "company": d["sequence_sets"]["company"] = json.loads(json.dumps(companies.DEFAULT_COMPANY_SEQUENCES))
        else: d["sequences"] = json.loads(json.dumps(outreach.DEFAULT_SEQUENCES))
        save(d)
    return {"ok": True}

@app.get("/api/preview/{k}")
def preview(k: str):
    d = load(); l = get_lead(d, k); seqs = outreach.seqs_for(d, l)
    return {"email": [dict(zip(("subject", "body"), outreach.render_email_step(l, d["settings"], seqs, i)), day=s["day"])
                      for i, s in enumerate(seqs["email"])],
            "whatsapp": [{"day": s["day"], "body": outreach.render(s["body"], l, d["settings"])} for s in seqs["whatsapp"]]}

@app.post("/api/search")
def search(req: SearchReq):
    d = load()
    if not discovery.keyword_providers(d["settings"]):
        raise HTTPException(400, "No search engine set up. On the server, SearXNG is included automatically. Or add a Firecrawl/Serper key in Settings. You can also use 'Find sites from free archives' below.")
    errs = []
    prov, hosts = discovery.search(d["settings"], req.query, errs.append)
    if not prov: raise HTTPException(400, "Search returned nothing. " + " ".join(errs))
    found = ["https://" + h for h in hosts]
    return {"urls": found, "new": [u for u in found if key_of(u) not in d["leads"]], "provider": prov}

_pool_busy = threading.Event()

def _pool_job(n):
    stopper.mark()
    try:
        with LOCK:
            d = load(); known = set(d["leads"]) | set(d.get("deleted", [])); due = discovery.bulk_due(d)
        if due:
            res = discovery.fetch_bulk(due, log)
            with LOCK:
                d = load(); discovery.merge_bulk(d, res, known); save(d)
        snap = load()
        picked = discovery.next_from_pool(snap, n, log)
        with LOCK:
            d = load(); pool = d.setdefault("candidates", {})
            for h, c in snap.get("candidates", {}).items():
                if c["status"] != "new" and h in pool: pool[h].update(c)
            save(d)
        picked = [u for u in picked if key_of(u) not in known]
        if picked and not stopper.stale(): scan_sync(picked)
        log(f"archive discovery: scanned {len(picked)} new sites")
    except Exception as e:
        log(f"archive discovery error: {e}")
    finally:
        _pool_busy.clear()

@app.post("/api/pool/run")
def pool_run(n: int = 20):
    """Pull builder-platform sites from the free archives, pre-check them, and fully scan the best n."""
    if _pool_busy.is_set(): raise HTTPException(400, "Already running")
    _pool_busy.set(); threading.Thread(target=_pool_job, args=(max(1, min(n, 200)),), daemon=True).start()
    return {"ok": True}

@app.post("/api/scan")
def scan(req: ScanReq):
    urls = []
    for u in req.urls:
        host = urlparse(u.strip() if "//" in u else "https://" + u.strip()).netloc
        if host and "https://" + host not in urls: urls.append("https://" + host)
    if not urls: raise HTTPException(400, "No valid URLs")
    with LOCK:                                   # scanning a site by hand brings it back if you'd deleted it
        d = load(); back = {key_of(u) for u in urls}
        if back & set(d.get("deleted", [])): d["deleted"] = [k for k in d["deleted"] if k not in back]; save(d)
    job = uuid.uuid4().hex[:8]
    with LOCK:
        d = load(); d["jobs"][job] = {"id": job, "total": len(urls), "done": 0, "current": None, "started": now(), "epoch": stopper.mine()}; save(d)
    threading.Thread(target=run_scan, args=(job, urls, req.pages), daemon=True).start()
    return {"job": job, "total": len(urls)}

@app.patch("/api/leads/{k}")
def patch_lead(k: str, p: LeadPatch):
    with LOCK:
        d = load(); l = get_lead(d, k)
        if p.status:
            l["status"] = p.status
            if p.status in ("won", "lost", "do_not_contact"): outreach.stop_all(l, p.status)
            if p.status == "do_not_contact":
                for c in (l.get("email"), l.get("number")):
                    if c and c not in d["suppression"]: d["suppression"].append(c)
        if p.notes is not None: l["notes"] = p.notes
        if p.email is not None: l["email"] = p.email.strip().lower() or None; l["email_locked"] = True
        if p.custom_name:
            l["custom_name"] = l["business"] = p.custom_name
            if l.get("opener"): l["opener"] = re.sub(r"^Hi, is this .+?\?", f"Hi, is this {p.custom_name}?", l["opener"])
        l["unread"] = False
        save(d); return l

def _delete_keys(d, keys):
    """Remove leads and remember them so Autopilot / the archives never add them back."""
    gone = 0
    dead = d.setdefault("deleted", [])
    for k in keys:
        l = d["leads"].pop(k, None)
        if l is None: continue
        gone += 1
        if k not in dead: dead.append(k)
        host = platforms.host_of(l.get("website") or l["scan"].get("site", ""))
        if host in d.get("candidates", {}): d["candidates"][host]["status"] = "skip"
    d["deleted"] = dead[-20000:]
    d["wa_outbox"] = [x for x in d.get("wa_outbox", []) if x["key"] not in keys]
    if WAWEB.get("pending") and WAWEB["pending"].get("key") in keys: WAWEB["pending"] = None
    return gone

@app.delete("/api/leads/{k}")
def delete_lead(k: str):
    with LOCK:
        d = load(); n = _delete_keys(d, [k]); save(d)
    log(f"🗑 deleted {n} lead")
    return {"ok": True, "deleted": n}

@app.post("/api/leads/delete")
def delete_many(body: dict):
    keys = [str(k) for k in (body.get("keys") or [])][:5000]
    if not keys: raise HTTPException(400, "Nothing selected")
    with LOCK:
        d = load(); n = _delete_keys(d, keys); save(d)
    log(f"🗑 deleted {n} leads")
    return {"ok": True, "deleted": n}

@app.post("/api/leads/{k}/sequence")
def sequence(k: str, r: SeqReq):
    with LOCK:
        d = load(); l = get_lead(d, k); s = l["seq"].get(r.channel)
        if r.action == "start":
            ok = outreach.can_email(l, d) if r.channel == "email" else outreach.can_whatsapp(l, d)
            if not ok: raise HTTPException(400, f"Lead can't be contacted by {r.channel} (no verified UK contact, suppressed or bounced)")
            other = l["seq"].get("whatsapp" if r.channel == "email" else "email", {})
            delay = d["settings"]["email"]["email_delay_days_if_whatsapp"] if r.channel == "email" and other.get("state") == "active" else 0
            outreach.start(l, r.channel, d, delay)
        elif s and r.action == "pause" and s["state"] == "active": s["state"] = "paused"
        elif s and r.action == "resume" and s["state"] == "paused":
            s["state"] = "active"
            if s.get("next_at") and outreach.parse(s["next_at"]) < outreach.uk_now(): s["next_at"] = outreach.iso(outreach.uk_now())
        elif s and r.action == "stop": s.update(state="stopped", stopped_reason="stopped by you", next_at=None)
        save(d)
    _wake.set(); return l

@app.post("/api/enroll")
def enroll(r: EnrollReq, min_quality: int = 0):
    """Start sequences for every eligible lead that hasn't been contacted yet."""
    n = {"email": 0, "whatsapp": 0}
    with LOCK:
        d = load(); _used = autopilot.already_contacted(d)
        for k, l in d["leads"].items():
            if r.keys and k not in r.keys: continue
            if l["status"] != "new": continue
            if l["scan"].get("competitor") or l["scan"].get("offline"): continue
            if min_quality and quality.score(l, d)["score"] < min_quality: continue
            cs = autopilot.contacts_of(l)
            if cs & _used: continue
            _used |= cs
            for ch in ("whatsapp", "email"):
                if ch not in r.channels or l["seq"].get(ch): continue
                if (ch == "email" and outreach.can_email(l, d)) or (ch == "whatsapp" and outreach.can_whatsapp(l, d) and l.get("opener")):
                    other = l["seq"].get("whatsapp", {})
                    delay = d["settings"]["email"]["email_delay_days_if_whatsapp"] if ch == "email" and other.get("state") == "active" else 0
                    outreach.start(l, ch, d, delay); n[ch] += 1
        save(d)
    _wake.set(); return n

@app.post("/api/leads/{k}/sent")
def sent(k: str, m: MsgReq):
    """You sent a WhatsApp message by hand. Logs it and moves the WhatsApp sequence forward."""
    with LOCK:
        d = load(); l = get_lead(d, k)
        l["history"].append({"from": "me", "channel": "whatsapp", "text": m.text, "at": now()})
        if l["status"] == "new": l["status"] = "contacted"
        s = l["seq"].get("whatsapp")
        if s and s["state"] == "active" and s.get("next_at") and outreach.parse(s["next_at"]) <= outreach.uk_now():
            outreach.advance(l, "whatsapp", outreach.seqs_for(d, l))
        elif not s and not any(h["from"] == "them" for h in l["history"]) and \
                len([h for h in l["history"] if h["from"] == "me" and h["channel"] == "whatsapp"]) == 1:
            outreach.start(l, "whatsapp", d); outreach.advance(l, "whatsapp", outreach.seqs_for(d, l))   # opener sent by hand -> follow-ups scheduled
        l["unread"] = False
        save(d); return l

@app.post("/api/leads/{k}/email")
def send_email_now(k: str, m: MsgReq):
    return _send_email_now(k, m)

def _send_email_now(k, m, attachments=None):
    """Send an email to the lead right now (manual reply, outside the sequence)."""
    d = load(); l = get_lead(d, k); es = d["settings"]["email"]
    if not outreach.email_ready(es): raise HTTPException(400, "Set up email in Settings first")
    if not l.get("email") or l["email"] in d["suppression"]: raise HTTPException(400, "No usable email for this lead")
    s = l["seq"].get("email", {}); ids = s.get("msgids", [])
    subj = m.subject or ("Re: " + s["subject"] if s.get("subject") else f"About the {l['business']} website")
    body = m.text.strip() + "\n\n--\n" + outreach.OPT_OUT
    try:
        mid = outreach.smtp_send(es, l["email"], subj, body, in_reply_to=ids[-1] if ids else None, references=ids,
                                 attachments=attachments)
    except Exception as e:
        raise HTTPException(400, f"Send failed: {e}")
    with LOCK:
        d = load(); l = d["leads"][k]
        l["seq"].setdefault("email", {"state": "manual", "step": 0, "msgids": [], "subject": subj})
        l["seq"]["email"].setdefault("msgids", []).append(mid)
        if not l["seq"]["email"].get("subject"): l["seq"]["email"]["subject"] = subj
        l["history"].append({"from": "me", "channel": "email", "subject": subj, "text": body, "at": now()})
        if l["status"] == "new": l["status"] = "contacted"
        l["unread"] = False
        d["sent_log"].append({"at": outreach.iso(outreach.uk_now()), "key": k, "to": l["email"], "manual": True}); save(d)
    return {"ok": True}

def make_draft(l, channel, intent=None, note=None):
    """Draft the next message, value-first (see DM_PLAYBOOK.md). Never mentions price before it's allowed."""
    d = load(); settings = d["settings"]
    last = next((h["text"] for h in reversed(l["history"]) if h["from"] == "them"), "")
    intent = intent or convo.classify(last)
    st = convo.stage(l)
    if intent in ("yes", "other") and not st["value_sent"]: st_name = "deliver_value"
    else: st_name = st["name"]
    price_ok = st["price_allowed"] or intent in ("price", "interested", "expensive", "close")
    medium = "a plain-text email body (no subject line, no signature)" if channel == "email" else "a WhatsApp message (2-5 short lines)"
    system = (dm_agent.PLAYBOOK + f"\n\n## Sender\nName: {dm_agent.ME['name']}. Portfolio: {dm_agent.ME['portfolio']}.\n"
              f"Draft the NEXT message as {medium}. Output only the message text. Don't repeat an introduction already made. "
              "If they opted out, output a one-line polite goodbye starting with [DNC].\n"
              f"\n## Right now\nSTAGE: {st_name}. {convo.STAGE_HINT.get(st_name, '')}\n"
              f"THEIR LAST MESSAGE MEANS: {intent}. {convo.INTENT_HINT.get(intent, '')}\n"
              + ("" if price_ok else "PRICE IS NOT ALLOWED in this message: no £, no packages, no 'I can do it for'.\n")
              + "\n" + convo.voice_prompt(settings.get("voice"))
              + (f"\n\n## PRIVATE instruction from the sender for THIS message (follow it; never mention or quote it in the reply)\n{note.strip()}"
                 if note and note.strip() else ""))
    facts = {"business": l["business"], "site": l["scan"]["site"], "entry_price_gbp": l["entry_price"],
             "verified_issues": [f"{i['issue']}: {i['detail']}" for i in l["scan"]["issues"]]}
    convo_txt = "\n".join(f"{h['from'].upper()} ({h.get('channel','whatsapp')}): {h['text'][:800]}" for h in l["history"][-12:])
    user = f"LEAD FACTS (only these are true):\n{json.dumps(facts, indent=1)}\n\nCONVERSATION:\n{convo_txt}\n\nDraft my next message."
    source = "ai"
    apply_settings(settings)
    try:
        draft = dm_agent.llm(system, user)
        if draft and not price_ok and re.search(r"£\s?\d", draft):        # the AI slipped in a price: try once more, firmly
            draft = dm_agent.llm(system + "\nYOUR LAST DRAFT MENTIONED A PRICE. REWRITE WITHOUT ANY PRICE.", user)
            if re.search(r"£\s?\d", draft or ""): draft = None
    except Exception as e:
        draft, source = None, f"template (AI error: {str(e)[:80]})"
    if not draft:
        source = source if source != "ai" else "template"
        draft = convo.template(l, intent if intent != "close" else "price", {**settings, "name": dm_agent.ME["name"]}, l["entry_price"])
    dnc = draft.startswith("[DNC]") or intent == "opt_out"
    return draft.replace("[DNC]", "").strip(), source, dnc, intent, st_name

def _autodraft(k, intent=None, note=None):
    try:
        with LOCK:
            d = load(); l = d["leads"].get(k)
        if not l: return
        last = next((h for h in reversed(l["history"]) if h["from"] == "them"), None)
        channel = (last or {}).get("channel", "whatsapp")
        if channel == "whatsapp" and not l.get("number"): channel = "email"
        text, source, dnc, intent, st_name = make_draft(l, channel, intent, note)
        with LOCK:
            d = load(); l = d["leads"][k]
            l["pending_draft"] = {"text": text, "channel": channel, "source": source, "intent": intent, "stage": st_name,
                                  "for": (last or {}).get("text", "")[:300], "at": now(), "dnc": dnc}
            save(d)
        log(f"✍️ reply drafted for {l['business']}, waiting for your OK")
        if st_name == "deliver_value" and not (DATA_DIR / "shots" / f"{k}-mobile.png").exists():
            threading.Thread(target=capture_shots, args=(k,), daemon=True).start()
    except Exception as e:
        log(f"draft error for {k}: {e}")

def queue_autodraft(k, intent=None):
    threading.Thread(target=_autodraft, args=(k, intent), daemon=True).start()

outreach.ON_REPLY = queue_autodraft

def capture_shots(k):
    """Phone + desktop screenshots of the lead's site: the 'value' you send when they say yes."""
    from playwright.sync_api import sync_playwright
    d = load(); l = d["leads"].get(k)
    if not l: return
    site = l.get("website") or l["scan"]["site"]
    out = DATA_DIR / "shots"; out.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            for kind, vp in (("mobile", {"width": 390, "height": 844}), ("desktop", {"width": 1366, "height": 860})):
                pg = br.new_page(viewport=vp, device_scale_factor=2 if kind == "mobile" else 1)
                pg.goto(site, wait_until="load", timeout=45000); pg.wait_for_timeout(2500)
                pg.screenshot(path=str(out / f"{k}-{kind}.png"))
                pg.close()
            br.close()
        log(f"📸 screenshots ready for {l['business']}")
    except Exception as e:
        log(f"screenshot failed for {l['business']}: {str(e)[:80]}")

@app.post("/api/leads/{k}/reply")
def reply(k: str, m: MsgReq):
    """Log a reply you received on WhatsApp (pasted in), stop follow-ups, and draft your answer."""
    with LOCK:
        d = load(); l = get_lead(d, k)
        l["history"].append({"from": "them", "channel": "whatsapp", "text": m.text, "at": now()})
        outreach.stop_all(l, "replied")
        if outreach.UNSUB_RE.search(m.text) and len(m.text) < 300:
            l["status"] = "do_not_contact"
            for c in (l.get("email"), l.get("number")):
                if c and c not in d["suppression"]: d["suppression"].append(c)
        elif l["status"] in ("new", "contacted", "later"): l["status"] = "talking"
        save(d)
    _autodraft(k)
    pdft = load()["leads"][k].get("pending_draft") or {}
    return {"draft": pdft.get("text", ""), "source": pdft.get("source"), "intent": pdft.get("intent"), "stage": pdft.get("stage"),
            "dnc": pdft.get("dnc") or l["status"] == "do_not_contact"}

@app.post("/api/leads/{k}/draft")
def draft_only(k: str, channel: str = "", intent: str = "", body: dict | None = None):
    """(Re)write the draft. intent = a quick-reply button, e.g. diy, no_worries, busy, price, offer, close."""
    d = load(); l = get_lead(d, k)
    if not channel:
        last = next((h for h in reversed(l["history"]) if h["from"] == "them"), None)
        channel = (last or {}).get("channel") or ("whatsapp" if l.get("number") else "email")
    note = (body or {}).get("note") or ""
    if not intent and (l.get("pending_draft") or {}).get("intent") and note: intent = l["pending_draft"]["intent"]   # keep the situation, change the style
    text, source, dnc, intent2, st_name = make_draft(l, channel, intent or None, note)
    with LOCK:
        d = load(); l = d["leads"][k]
        l["pending_draft"] = {"text": text, "channel": channel, "source": source, "intent": intent2, "stage": st_name, "note": note,
                              "for": next((h["text"] for h in reversed(l["history"]) if h["from"] == "them"), "")[:300], "at": now(), "dnc": dnc}
        if (body or {}).get("remember") and note.strip():          # "use this for all future replies"
            v = d["settings"].setdefault("voice", dict(convo.DEFAULT_VOICE))
            v["notes"] = (v.get("notes", "").rstrip() + "\n- " + note.strip()).strip()
        save(d)
    return {"draft": text, "source": source, "dnc": dnc, "intent": intent2, "stage": st_name, "channel": channel}

class ApproveReq(BaseModel):
    text: str; channel: str = "whatsapp"; subject: str | None = None; attach: bool = False

@app.post("/api/voice/test")
def voice_test(body: dict):
    """Try your voice settings: draft a reply to a sample message, without touching any lead."""
    d = load(); msg = (body.get("message") or "Yes please, what did you find?").strip()
    base = next((l for l in d["leads"].values() if l["scan"].get("is_uk") and l["scan"]["issues"]), None)
    if not base: raise HTTPException(400, "Scan at least one lead first")
    fake = json.loads(json.dumps(base))
    fake["history"] = [{"from": "me", "channel": "whatsapp", "text": base.get("opener") or "Hi, quick heads-up about your website. Want me to send a screenshot?", "at": now()},
                       {"from": "them", "channel": "whatsapp", "text": msg, "at": now()}]
    fake.pop("value_sent", None); fake["status"] = "talking"
    text, source, dnc, intent, st_name = make_draft(fake, "whatsapp", None, body.get("note"))
    return {"draft": text, "source": source, "intent": intent, "stage": st_name, "lead": base["business"]}

@app.post("/api/leads/{k}/approve")
def approve(k: str, r: ApproveReq):
    """You checked the draft: send it. WhatsApp goes through your extension, email through your mailbox."""
    text = r.text.strip()
    if not text: raise HTTPException(400, "The message is empty")
    d = load(); l = get_lead(d, k); pd_ = l.get("pending_draft") or {}
    if r.channel == "email":
        shots = [str(DATA_DIR / "shots" / f"{k}-{x}.png") for x in ("mobile", "desktop")] if r.attach else []
        shots = [p for p in shots if os.path.exists(p)]
        res = _send_email_now(k, MsgReq(text=text, channel="email", subject=r.subject), attachments=shots)
        with LOCK:
            d = load(); l = d["leads"][k]; l.pop("pending_draft", None); l["unread"] = False
            if pd_.get("stage") == "deliver_value": l["value_sent"] = True
            if pd_.get("dnc"): l["status"] = "do_not_contact"
            save(d)
        return {"sent": "email", **res}
    if not l.get("number"): raise HTTPException(400, "This lead has no WhatsApp number")
    connected = time.time() - WAWEB["last_seen"] < 90 and d["settings"]["whatsapp"].get("sender", "browser") == "browser"
    with LOCK:
        d = load(); l = d["leads"][k]
        if pd_.get("stage") == "deliver_value": l["value_sent"] = True
        if pd_.get("dnc"): l["status"] = "do_not_contact"
        l.pop("pending_draft", None); l["unread"] = False
        if connected:
            d.setdefault("wa_outbox", []).append({"id": uuid.uuid4().hex[:10], "key": k, "number": l["number"].lstrip("+"),
                                                  "text": text, "business": l["business"]})
        save(d)
    if connected:
        log(f"👍 reply to {l['business']} approved, sending through WhatsApp Web")
        return {"sent": "queued"}
    return {"sent": "manual", "link": f"https://wa.me/{l['number'].lstrip('+')}?text={urllib.parse.quote(text)}"}

@app.post("/api/leads/{k}/draft/discard")
def discard_draft(k: str):
    with LOCK:
        d = load(); l = get_lead(d, k); l.pop("pending_draft", None); l["unread"] = False; save(d)
    return {"ok": True}

@app.post("/api/leads/{k}/shots")
def shots(k: str):
    get_lead(load(), k); threading.Thread(target=capture_shots, args=(k,), daemon=True).start()
    return {"ok": True}

@app.get("/shots/{name}")
def shot_file(name: str):
    from fastapi.responses import FileResponse
    p = DATA_DIR / "shots" / re.sub(r"[^a-z0-9._-]", "", name.lower())
    if not p.exists(): raise HTTPException(404)
    return FileResponse(p, media_type="image/png")

@app.get("/api/today")
def today():
    d = load(); nowuk = outreach.uk_now(); wa_due, upcoming = [], []
    for l in d["leads"].values():
        s = l["seq"].get("whatsapp")
        if s and s["state"] == "active" and s.get("next_at") and outreach.can_whatsapp(l, d) and l["status"] not in ("talking", "won", "lost"):
            if outreach.parse(s["next_at"]) <= nowuk:
                wa_due.append({"auto": d["settings"]["whatsapp"]["auto"], "key": l["key"], "business": l["business"], "number": l["number"], "step": s["step"],
                               "total": len(outreach.seqs_for(d, l)["whatsapp"]),
                               "text": outreach.render(outreach.seqs_for(d, l)["whatsapp"][s["step"]]["body"], l, d["settings"])})
        for ch in ("email", "whatsapp"):
            s = l["seq"].get(ch)
            if s and s["state"] == "active" and s.get("next_at") and outreach.parse(s["next_at"]) > nowuk:
                upcoming.append({"key": l["key"], "business": l["business"], "channel": ch, "step": s["step"] + 1, "at": s["next_at"]})
    upcoming.sort(key=lambda x: x["at"])
    drafts = sorted([{"key": l["key"], "business": l["business"], **l["pending_draft"], "number": l.get("number"), "email": l.get("email"),
                      "shots": [f"/shots/{l['key']}-{x}.png" for x in ("mobile", "desktop") if (DATA_DIR / "shots" / f"{l['key']}-{x}.png").exists()],
                      "subject": "Re: " + l["seq"]["email"]["subject"] if l.get("seq", {}).get("email", {}).get("subject") else ""}
                     for l in d["leads"].values() if l.get("pending_draft")], key=lambda x: x["at"], reverse=True)
    replies = [{"key": l["key"], "business": l["business"], "channel": next((h["channel"] for h in reversed(l["history"]) if h["from"] == "them"), ""),
                "text": next((h["text"] for h in reversed(l["history"]) if h["from"] == "them"), "")[:200]}
               for l in d["leads"].values() if l.get("unread")]
    recent = sorted(d["sent_log"], key=lambda x: x["at"], reverse=True)[:15]
    for r in recent: r["business"] = d["leads"].get(r["key"], {}).get("business", r["key"])
    return {"drafts": drafts, "outbox": len(d.get("wa_outbox", [])), "replies": [r for r in replies if not any(x["key"] == r["key"] for x in drafts)], "wa_due": wa_due, "upcoming": upcoming[:25], "recent": recent,
            "events": d["events"][-20:][::-1]}

# ================= Companies (directory leads, e.g. Europages) =================
class CoParse(BaseModel):
    text: str
class CoRun(BaseModel):
    companies: list[dict]; pages: int = 2

@app.post("/api/co/parse")
def co_parse(req: CoParse):
    d = load(); names = {l.get("business", "").lower() for l in d["leads"].values() if l.get("source") == "company"}
    names |= {u["name"].lower() for u in d["co_unresolved"]}
    out = companies.parse_paste(req.text)
    for c in out: c["known"] = c["name"].lower() in names
    return {"companies": out}

def build_company_lead(c, website, how, conf, raw, info, site_issues, existing=None):
    keep = {"fake_phone_number", "broken_call_button", "fake_email", "empty_social_link", "broken_whatsapp_button",
            "placeholder_text", "dead_button", "whatsapp_button_on_landline"}
    issues = site_issues + [i for i in raw["issues"] if i["issue"] in keep]
    raw = {**raw, "issues": issues, "is_uk": raw["is_uk"] or c.get("country", "").lower() in ("united kingdom", "uk", "england", "scotland", "wales", "northern ireland"),
           "unfinished_score": sum(companies.ISSUE_WEIGHT.get(i["issue"], 1) for i in issues)}
    uk_wa = [w for w in raw["whatsapp"] if w["number"].startswith("+44")]
    hook, bullets = companies.hook_and_bullets(issues)
    lead = existing or {"status": "new", "history": [], "notes": "", "seq": {}}
    lead.update({"key": companies.company_key(website, c["name"]), "source": "company", "sequence_set": "company",
                 "business": lead.get("custom_name") or companies.short_name(c["name"]), "company": c, "website": website, "found_by": how, "match": conf,
                 "audit": info, "scan": raw, "hook": None, "hook_text": hook, "bullets": bullets,
                 "number": uk_wa[0]["number"] if uk_wa else None, "scanned_at": now()})
    lead["entry_price"] = load()["settings"].get("company_price", 495)
    if not lead.get("email_locked"): lead["email"] = outreach.best_email(lead)
    dd = load(); lead["opener"] = outreach.render(outreach.seqs_for(dd, lead)["whatsapp"][0]["body"], lead, dd["settings"])
    return lead

def run_companies(job_id, items, pages):
    from playwright.sync_api import sync_playwright
    d = load(); settings = d["settings"]
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(user_agent=companies.UA, viewport={"width": 1280, "height": 900})
        for i, c in enumerate(items):
            if job_stopped(job_id): break
            with LOCK:
                d = load(); JOB(d, job_id).update(current=c["name"], done=i); save(d)
            try:
                if c.get("website"):             # taken from the directory page you were viewing: exact
                    website, how, conf = companies.normalise_site(c["website"]), c.get("found") or "from directory page", 1.0
                else:
                    website, how, conf = companies.find_website(c, settings)
                if not website:
                    with LOCK:
                        d = load(); d["co_unresolved"] = [u for u in d["co_unresolved"] if u["name"] != c["name"]] + [{**c, "reason": how, "at": now()}]; save(d)
                    continue
                fc_key = load()["settings"].get("firecrawl_key")
                if load()["settings"]["co_auto"].get("scan_with") == "firecrawl" and fc_key:
                    fpages = companies.fc_scan_site(fc_key, website, pages)          # ~1 Firecrawl credit per page
                    raw = scout.analyse(website, fpages)
                    home = next((x for x in fpages if "error" not in x), {"raw": "", "url": website})
                    info, site_issues = companies.audit_html(home.get("raw", ""), home.get("url", website))
                else:
                    raw = scout.analyse(website, scout.scan_site(pg, website, pages))   # free, Scout's own browser
                    info, site_issues = companies.audit(pg, website)
                with LOCK:
                    d = load(); k = companies.company_key(website, c["name"])
                    d["leads"][k] = build_company_lead(c, website, how, conf, raw, info, site_issues, d["leads"].get(k))
                    d["co_unresolved"] = [u for u in d["co_unresolved"] if u["name"] != c["name"]]
                    save(d)
            except Exception as e:
                with LOCK:
                    d = load(); JOB(d, job_id).setdefault("errors", []).append(f"{c['name']}: {str(e)[:100]}"); save(d)
        br.close()
    with LOCK:
        d = load(); JOB(d, job_id).update(current=None, finished=now(), **({} if JOB(d, job_id).get("cancelled") else {"done": len(items)})); save(d)
    log(f"companies: finished {len(items)}")

def _known_company_domains(d):
    doms = {platforms.host_of(l.get("website", "")) for l in d["leads"].values() if l.get("source") == "company"}
    doms |= {k[3:].replace("-", ".") for k in d.get("deleted", []) if k.startswith("co-")}
    return doms

def find_companies(sector, towns, per_search=10, log_=None):
    """Firecrawl search for each town -> new UK company websites (not already in Scout)."""
    d = load(); key = d["settings"].get("firecrawl_key")
    if not key: raise HTTPException(400, "Add your Firecrawl API key in Settings first")
    known, found = _known_company_domains(d), []
    for town in towns:
        if stopper.stale(): break
        try:
            res = companies.fc_search_companies(key, sector, town, per_search)
        except Exception as e:
            log(f"Firecrawl search failed for {sector} {town}: {str(e)[:80]}"); continue
        new = [c for c in res if companies.domain_of(c["website"]) not in known and companies.company_key(c["website"], c["name"]) not in d.get("deleted", [])]
        known |= {companies.domain_of(c["website"]) for c in new}
        found += new
        with LOCK:
            d = load(); d.setdefault("co_queries_used", {})[f"{sector}|{town}"] = autopilot.today()
            day = d.setdefault("co_days", {}).setdefault(autopilot.today(), {"searches": 0, "found": 0, "started": 0})
            day["searches"] += 1; day["found"] += len(new); save(d)
        log(f"🔎 Firecrawl: {sector} in {town} -> {len(res)} results, {len(new)} new companies")
    return found

class CoFind(BaseModel):
    sector: str; towns: list[str]; per_search: int = 10; pages: int = 2

@app.post("/api/co/find")
def co_find(req: CoFind):
    """Find UK companies with Firecrawl, then check each website and pull contacts. Runs in the background."""
    if not load()["settings"].get("firecrawl_key"): raise HTTPException(400, "Add your Firecrawl API key in Settings first")
    towns = [t.strip() for t in req.towns if t.strip()][:30] or ["UK"]
    job = uuid.uuid4().hex[:8]
    with LOCK:
        d = load(); d["jobs"][job] = {"id": job, "total": 0, "done": 0, "current": f"searching {req.sector}…", "started": now(), "epoch": stopper.mine(), "kind": "companies"}; save(d)
    def go():
        stopper.mark()
        items = find_companies(req.sector.strip(), towns, max(1, min(req.per_search, 30)))
        with LOCK:
            d = load(); JOB(d, job)["total"] = len(items); save(d)
        if items and not stopper.stale(): run_companies(job, items, req.pages)
        else:
            with LOCK:
                d = load(); JOB(d, job).update(finished=now(), current=None); save(d)
    threading.Thread(target=go, daemon=True).start()
    return {"job": job}

def co_autopilot_pass():
    """Company Autopilot: daily target of new UK companies found, checked and (optionally) started."""
    d = load(); ca = d["settings"]["co_auto"]
    if not ca.get("enabled") or not d["settings"].get("firecrawl_key") or stopper.stale(): return
    nowuk = outreach.uk_now()
    if d["settings"]["email"].get("weekdays_only", True) and nowuk.weekday() >= 5: return
    if not (7 <= nowuk.hour < int(d["settings"]["email"].get("end_hour", 17))): return
    day = d.get("co_days", {}).get(autopilot.today(), {"searches": 0, "found": 0, "started": 0})
    if day["found"] >= ca["daily_target"] or day["searches"] >= ca["max_searches_per_day"]: return
    used = d.get("co_queries_used", {}); cutoff = (nowuk - datetime.timedelta(days=30)).date().isoformat()
    combo = next(((s, t) for t in ca["towns"] for s in ca["sectors"] if used.get(f"{s}|{t}", "") < cutoff), None)
    if not combo: log("company autopilot: every sector × town searched in the last 30 days. Add more towns or sectors"); return
    items = find_companies(combo[0], [combo[1]], int(ca["per_search"]))[: max(0, ca["daily_target"] - day["found"]) or None]
    if stopper.stale(): return
    if items:
        job = uuid.uuid4().hex[:8]
        with LOCK:
            d = load(); d["jobs"][job] = {"id": job, "total": len(items), "done": 0, "current": None, "started": now(), "epoch": stopper.mine(), "kind": "companies", "auto": True}; save(d)
        run_companies(job, items, 2)
    if stopper.stale(): return
    if ca.get("auto_outreach"):
        with LOCK:
            d = load(); n = 0; used_c = autopilot.already_contacted(d)
            for k, l in d["leads"].items():
                if l.get("source") != "company" or l["status"] != "new" or l["seq"].get("email"): continue
                if l["scan"].get("unfinished_score", 0) < ca["min_score"] or not outreach.can_email(l, d): continue
                if quality.score(l, d)["score"] < ca.get("min_quality", 60): continue
                if autopilot.contacts_of(l) & used_c: continue
                used_c |= autopilot.contacts_of(l); outreach.start(l, "email", d); n += 1
            if n:
                d.setdefault("co_days", {}).setdefault(autopilot.today(), {"searches": 0, "found": 0, "started": 0})["started"] += n
            save(d)
        if n: log(f"company autopilot: started email outreach for {n} companies with poor websites"); _wake.set()

@app.post("/api/co/run")
def co_run(req: CoRun):
    items = [c for c in req.companies if c.get("name")][:300]
    if not items: raise HTTPException(400, "No companies selected")
    job = uuid.uuid4().hex[:8]
    with LOCK:
        d = load(); d["jobs"][job] = {"id": job, "total": len(items), "done": 0, "current": None, "started": now(), "epoch": stopper.mine(), "kind": "companies"}; save(d)
    threading.Thread(target=run_companies, args=(job, items, req.pages), daemon=True).start()
    return {"job": job, "total": len(items)}

@app.get("/collector.js")
def collector_js(request: Request):
    """The 'Add to Scout' bookmarklet code, pointed at this Scout (laptop or server)."""
    js = (HERE / "collector.js").read_text(encoding="utf-8").replace("__SCOUT_ORIGIN__", str(request.base_url).rstrip("/"))
    return HTMLResponse(js, media_type="application/javascript")

@app.post("/api/co/enroll")
def co_enroll(r: EnrollReq, min_score: int = 3, min_quality: int = 0):
    """Start company sequences. Skips companies whose website scored below min_score (their site is fine)."""
    n = {"email": 0, "whatsapp": 0}
    with LOCK:
        d = load()
        for k, l in d["leads"].items():
            if l.get("source") != "company" or l["status"] != "new" or (r.keys and k not in r.keys): continue
            if not r.keys and l["scan"].get("unfinished_score", 0) < min_score: continue
            if not r.keys and min_quality and quality.score(l, d)["score"] < min_quality: continue
            for ch in r.channels:
                if l["seq"].get(ch): continue
                if (ch == "email" and outreach.can_email(l, d)) or (ch == "whatsapp" and outreach.can_whatsapp(l, d)):
                    outreach.start(l, ch, d); n[ch] += 1
        save(d)
    _wake.set(); _wa_wake.set(); return n

@app.post("/api/co/settings")
def co_settings(body: dict):
    with LOCK:
        d = load()
        if body.get("company_price"): d["settings"]["company_price"] = max(0, int(body["company_price"]))
        for l in d["leads"].values():
            if l.get("source") == "company": l["entry_price"] = d["settings"]["company_price"]
        if body.get("clear_unresolved"): d["co_unresolved"] = []
        save(d)
    return {"ok": True}

@app.post("/api/import")
def import_json(path: str = "leads.json"):
    n = 0
    with LOCK:
        d = load()
        for f in path.split(","):
            for raw in json.load(open(HERE / f.strip())):
                k = key_of(raw["site"]); d["leads"][k] = build_lead(raw, d["leads"].get(k)); n += 1
        save(d)
    return {"imported": n}

if __name__ == "__main__":
    import uvicorn, webbrowser
    host = os.environ.get("HOST", "127.0.0.1"); port = int(os.environ.get("PORT", "8000"))
    if host != "127.0.0.1" and not PASSWORD:
        sys.exit("Refusing to run on a public address without SCOUT_PASSWORD set. Set it and try again.")
    apply_settings(load()["settings"])
    start_wa_service()
    if load()["settings"].get("engine_paused"): stop_everything("Scout was paused last time")   # old Pause state: turn it into a clean STOP
    threading.Thread(target=engine_loop, daemon=True).start()
    threading.Thread(target=wa_loop, daemon=True).start()
    threading.Thread(target=autopilot_loop, daemon=True).start()
    if "--no-browser" not in sys.argv and host == "127.0.0.1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    print(f"\n  Scout is running at http://{'localhost' if host == '127.0.0.1' else host}:{port}  (keep this running)\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
