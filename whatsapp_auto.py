"""
WhatsApp auto-sender. Sends due WhatsApp sequence steps through wa-service, never more than 100/day.

Safety built in:
  * HARD_MAX = 100 per day, whatever the settings say
  * warm-up: a new number starts at 10/day and rises by 5 each sending day
  * random 2-7 minute gaps, a "typing…" pause before each message, UK working hours only
  * numbers not on WhatsApp are skipped and marked; replies stop everything (handled in app.py)
"""
import json, random, time, datetime, urllib.request
import outreach, stopper

HARD_MAX = 100
DEFAULT_WA = {"auto": False, "daily_cap": 40, "start_hour": 9, "end_hour": 18, "weekdays_only": True,
              "gap_min_s": 120, "gap_max_s": 420, "warmup": True,
              "sender": "browser"}   # "browser" = your own WhatsApp Web via the Chrome extension, "service" = built-in (QR scan)

class WA:
    def __init__(self, url, secret):
        self.url, self.secret = url.rstrip("/"), secret
    def _req(self, path, data=None, timeout=60):
        r = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                                   headers={"Content-Type": "application/json", "x-scout-secret": self.secret},
                                   method="POST" if data is not None else "GET")
        try:
            return json.load(urllib.request.urlopen(r, timeout=timeout))
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}") or {"error": f"http {e.code}"}
    def status(self):
        try: return self._req("/status", timeout=5)
        except Exception as e: return {"state": "offline", "lastError": str(e)[:120]}
    def send(self, to, text): return self._req("/send", {"to": to, "text": text}, timeout=90)
    def logout(self): return self._req("/logout", {})

PROGRESS = {"active": False, "total": 0, "done": 0, "failed": 0, "current": None, "next_at": None,
            "queue": [], "results": [], "started": None, "finished": None, "stopped": None}

def settings(d):
    return {**DEFAULT_WA, **d["settings"].get("whatsapp", {})}

def sent_today(d):
    t = outreach.uk_now().date().isoformat()
    return sum(1 for x in d.get("wa_log", []) if x["at"][:10] == t)

def effective_cap(d, ws):
    cap = min(int(ws["daily_cap"]), HARD_MAX)
    if ws.get("warmup", True):
        days = sorted({x["at"][:10] for x in d.get("wa_log", [])})
        today = outreach.uk_now().date().isoformat()
        sending_days = len([x for x in days if x < today])
        cap = min(cap, 10 + 5 * sending_days)
    return cap

def in_window(ws):
    now = outreach.uk_now()
    if ws.get("weekdays_only", True) and now.weekday() >= 5: return False
    return int(ws["start_hour"]) <= now.hour < int(ws["end_hour"])

def due(d):
    now = outreach.uk_now(); out = []
    for k, l in d["leads"].items():
        s = l.get("seq", {}).get("whatsapp")
        if s and s["state"] == "active" and s.get("next_at") and outreach.parse(s["next_at"]) <= now \
                and outreach.can_whatsapp(l, d) and l.get("wa_status") != "not_on_whatsapp" \
                and l["status"] not in ("talking", "won", "lost", "do_not_contact"):
            out.append(k)
    out.sort(key=lambda k: (d["leads"][k]["seq"]["whatsapp"]["step"] == 0, d["leads"][k]["seq"]["whatsapp"]["next_at"]))
    return out   # follow-ups before new openers, oldest first

def tick(load, save, lock, log, wa):
    e0 = stopper.mark()
    with lock:
        d = load(); ws = settings(d)
    if ws.get("sender", "browser") != "service": return        # the Chrome extension does the sending
    if not ws["auto"] or not in_window(ws): return
    st = wa.status()
    if st.get("state") != "ready":
        with lock:
            d = load(); d.setdefault("engine", {})["wa_error"] = f"WhatsApp not connected ({st.get('state')})"; save(d)
        return
    with lock:
        d = load(); ws = settings(d)
        planned = max(0, min(len(due(d)), effective_cap(d, ws) - sent_today(d)))
    if not planned: return
    PROGRESS.update(active=True, total=planned, done=0, failed=0, current=None, next_at=None, results=[],
                    started=outreach.iso(outreach.uk_now()), finished=None, stopped=None)
    try:
        while True:
            with lock:
                d = load(); ws = settings(d)
                if not ws["auto"] or not in_window(ws) or stopper.stale(e0): return
                if sent_today(d) >= effective_cap(d, ws): return
                todo = due(d)
                if not todo: return
                PROGRESS["queue"] = [d["leads"][x]["business"] for x in todo[1:PROGRESS["total"] - PROGRESS["done"]]]
                k = todo[0]; l = d["leads"][k]; s = l["seq"]["whatsapp"]
                text = outreach.render(outreach.seqs_for(d, l)["whatsapp"][s["step"]]["body"], l, d["settings"])
                number, step, biz = l["number"], s["step"], l["business"]
            if not text.strip():
                with lock:
                    d = load(); outreach.advance(d["leads"][k], "whatsapp", outreach.seqs_for(d, d["leads"][k])); save(d)
                continue
            PROGRESS.update(current={"business": biz, "to": number, "step": step + 1}, next_at=None)
            r = wa.send(number, text)
            with lock:
                d = load(); l = d["leads"][k]
                if r.get("ok"):
                    l["history"].append({"from": "me", "channel": "whatsapp", "text": text, "auto": True, "step": step,
                                         "at": datetime.datetime.now().isoformat(timespec="seconds")})
                    if l["status"] == "new": l["status"] = "contacted"
                    outreach.advance(l, "whatsapp", outreach.seqs_for(d, l))
                    d.setdefault("wa_log", []).append({"at": outreach.iso(outreach.uk_now()), "key": k, "step": step})
                    d["wa_log"] = d["wa_log"][-5000:]
                    d.setdefault("engine", {})["wa_error"] = None
                    PROGRESS["done"] += 1
                    log(f"📱 sent WhatsApp {step+1} to {biz} ({sent_today(d)}/{effective_cap(d, settings(d))} today)")
                elif r.get("error") == "not_on_whatsapp":
                    l["wa_status"] = "not_on_whatsapp"
                    l["seq"]["whatsapp"].update(state="stopped", stopped_reason="number not on WhatsApp", next_at=None)
                    PROGRESS["failed"] += 1
                    log(f"⚠️ {biz}: {number} is not on WhatsApp, skipped")
                else:
                    d.setdefault("engine", {})["wa_error"] = f"send failed: {r}"
                    save(d); log(f"⚠️ WhatsApp to {biz} failed: {r}"); PROGRESS["stopped"] = "WhatsApp send failed"; return
                save(d)
            PROGRESS["results"].insert(0, {"business": biz, "to": number, "step": step + 1, "ok": bool(r.get("ok")),
                                           "at": outreach.iso(outreach.uk_now()), "note": "" if r.get("ok") else "not on WhatsApp"})
            del PROGRESS["results"][20:]
            PROGRESS["current"] = None
            if r.get("ok"):
                gap = random.uniform(float(ws["gap_min_s"]), float(ws["gap_max_s"]))
                PROGRESS["next_at"] = outreach.iso(outreach.uk_now() + datetime.timedelta(seconds=gap))
                if stopper.sleep(gap, e0): PROGRESS["stopped"] = "stopped by you"; return
    finally:
        PROGRESS.update(active=False, current=None, next_at=None, queue=[], finished=outreach.iso(outreach.uk_now()))
