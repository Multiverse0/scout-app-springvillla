"""
DM Agent v1 - drafts WhatsApp messages from verified lead data. YOU send them (protects your number).

Commands:
  python dm_agent.py openers leads.json      -> outreach.csv: first message + click-to-send WhatsApp link per lead
  python dm_agent.py reply <lead> "<their message>"   -> logs their message, drafts your reply (AI)
  python dm_agent.py sent  <lead> "<what you sent>"   -> logs what you actually sent
  python dm_agent.py status <lead> do_not_contact|won|lost|later
  python dm_agent.py today                -> who needs a follow-up today

<lead> = the site's short name, e.g. "reepro" for reepro.base44.app

AI setup (pick one, set as environment variable):
  GEMINI_API_KEY=...      (optional GEMINI_MODEL, default gemini-2.5-flash)
  ANTHROPIC_API_KEY=...   (optional ANTHROPIC_MODEL)
Without a key, `reply` falls back to the playbook templates.
"""
import json, os, re, sys, datetime, csv, urllib.request, urllib.parse
from pathlib import Path

ME = {"name": "YOUR_NAME", "portfolio": "YOUR_PORTFOLIO_LINK"}   # <-- fill these in
HERE = Path(__file__).parent
DB = HERE / "conversations.json"
PLAYBOOK = (HERE / "DM_PLAYBOOK.md").read_text(encoding="utf-8")
TODAY = datetime.date.today()

def load_db():
    return json.loads(DB.read_text()) if DB.exists() else {}

def save_db(db):
    DB.write_text(json.dumps(db, indent=2))

def key_of(site):
    return site.split("//")[-1].split(".")[0]

def business_name(lead):
    t = re.split(r"\s[|—–-]\s", lead["title"])[0].strip()
    return t if 2 < len(t) < 40 else key_of(lead["site"])

def pretty_uk(e164):
    d = "0" + e164[3:]
    return f"{d[:5]} {d[5:]}" if d.startswith("07") else d

# ---------- Stage 1: opener built ONLY from verified issues ----------
PRIORITY = ["broken_whatsapp_button", "fake_phone_number", "broken_call_button", "fake_email",
            "placeholder_text", "whatsapp_button_on_landline", "empty_social_link", "no_whatsapp_button"]

def hook_sentence(issue):
    k, d = issue["issue"], issue["detail"]
    num = re.search(r"(?:wa\.me/|tel:)\+?(\d{9,13})", d)
    shown = pretty_uk("+" + num.group(1)) if num and num.group(1).startswith("44") else (num.group(1) if num else "")
    if k == "broken_whatsapp_button":
        return f"the WhatsApp button on your website goes to {shown}, which isn't a real number, so anyone tapping it can't reach you."
    if k == "fake_phone_number":
        return f"the call button on your website dials {shown}, which looks like a placeholder number, so callers can't get through."
    if k == "broken_call_button":
        return "the call button on your website doesn't have a number in it, so tapping it does nothing."
    if k == "fake_email":
        addr = d.split(" -> ")[0]
        return f"the email on your website ({addr}) can't receive messages, so enquiries sent there are bouncing."
    if k == "placeholder_text":
        txt = re.search(r"'(.+?)'", d)
        return f"your website still shows template text ({txt.group(1) if txt else 'placeholder text'}), which can put customers off booking."
    if k == "whatsapp_button_on_landline":
        return "the WhatsApp button on your website points to your landline, which usually can't receive WhatsApp, so those messages never arrive."
    if k == "empty_social_link":
        net = d.split(" ")[0].capitalize()
        return f"the {net} icon on your website just opens the {net} homepage instead of your page, so customers can't find you."
    if k == "no_whatsapp_button":
        return "your website doesn't have a WhatsApp button, and most customers would rather message than call."
    return None

def opener(lead):
    issues = sorted([i for i in lead["issues"] if i["issue"] in PRIORITY], key=lambda i: PRIORITY.index(i["issue"]))
    if not issues:
        return None, None
    biz = business_name(lead)
    msg = (f"Hi, is this {biz}? Hope you don't mind me messaging. I was having a look at your website and noticed "
           f"{hook_sentence(issues[0]).replace(' on your website', '', 1)} Would it help if I sent you a screenshot?")
    return msg, issues[0]["issue"]

def entry_price(lead):
    kinds = {i["issue"] for i in lead["issues"]}
    contact = kinds & {"broken_whatsapp_button", "fake_phone_number", "broken_call_button", "fake_email", "whatsapp_button_on_landline", "no_whatsapp_button"}
    social = kinds & {"empty_social_link"}
    content = kinds & {"placeholder_text", "dead_button"}
    if contact and social and content: return 150
    if (contact and social) or (contact and content) or (social and content): return 100
    return 50

def wa_link(number, text):
    return f"https://wa.me/{number.lstrip('+')}?text={urllib.parse.quote(text)}"

def cmd_openers(*paths):
    leads = [l for p in paths for l in json.load(open(p))]
    db = load_db()
    rows = []
    for l in leads:
        k = key_of(l["site"])
        l["whatsapp"] = [w for w in l["whatsapp"] if w["number"].startswith("+44")]   # UK numbers only
        if not l.get("is_uk") or not l["whatsapp"] or db.get(k, {}).get("status") == "do_not_contact":
            continue
        msg, hook = opener(l)
        if not msg:
            continue
        num = l["whatsapp"][0]["number"]
        db.setdefault(k, {"site": l["site"], "business": business_name(l), "number": num, "status": "new",
                          "entry_price": entry_price(l), "verified_issues": l["issues"], "history": []})
        rows.append([k, business_name(l), num, l["whatsapp"][0]["basis"], hook, f"£{entry_price(l)}", msg, wa_link(num, msg)])
    save_db(db)
    with open(HERE / "outreach.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lead", "business", "whatsapp", "number_basis", "hook_issue", "entry_price", "opener", "click_to_send"])
        w.writerows(rows)
    for r in rows:
        print(f"\n[{r[0]}] {r[1]}  {r[2]}  (entry {r[5]})\n  {r[6]}")
    print(f"\n{len(rows)} openers -> outreach.csv. Open each link, check, send by hand, then run: dm_agent.py sent <lead> \"...\"")

# ---------- Stages 2-5: AI reply drafting ----------
_GEMINI = {"model": None}
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"

def gemini_pick_model(key):
    """Ask Google which models this key can use and pick the best 'flash' one, so Scout keeps working
    when Google retires a model name."""
    req = urllib.request.Request(f"{GEMINI_API}/models?pageSize=200", headers={"x-goog-api-key": key})
    models = json.load(urllib.request.urlopen(req, timeout=30)).get("models", [])
    names = [m["name"].split("/", 1)[-1] for m in models if "generateContent" in m.get("supportedGenerationMethods", [])]
    def rank(n):
        ver = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        v = float(ver.group(1)) if ver else 0
        return ("flash" in n and "lite" not in n, "preview" not in n and "exp" not in n, "image" not in n and "tts" not in n
                and "audio" not in n and "live" not in n, v, -len(n))
    gem = sorted([n for n in names if n.startswith("gemini")], key=rank, reverse=True)
    if not gem: raise RuntimeError("this Gemini key has no text models available")
    return gem[0]

def _gemini_call(key, model, system, user):
    body = {"system_instruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}]}
    req = urllib.request.Request(f"{GEMINI_API}/models/{model}:generateContent", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    r = json.load(urllib.request.urlopen(req, timeout=60))
    parts = r.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()

def llm(system, user):
    if os.getenv("GEMINI_API_KEY"):
        key = os.environ["GEMINI_API_KEY"]
        model = os.getenv("GEMINI_MODEL") or _GEMINI["model"] or gemini_pick_model(key)
        try:
            out = _gemini_call(key, model, system, user)
        except urllib.error.HTTPError as e:
            if e.code != 404 or os.getenv("GEMINI_MODEL"): raise
            model = gemini_pick_model(key)               # model was retired: pick a current one and retry once
            out = _gemini_call(key, model, system, user)
        _GEMINI["model"] = model
        return out
    if os.getenv("ANTHROPIC_API_KEY"):
        body = {"model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"), "max_tokens": 600, "system": system,
                "messages": [{"role": "user", "content": user}]}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                              "anthropic-version": "2023-06-01"})
        return json.load(urllib.request.urlopen(req, timeout=60))["content"][0]["text"].strip()
    return None

def template_reply(c, msg):
    m = msg.lower()
    if re.search(r"\b(stop|no thanks|not interested|unsubscribe|leave me|remove)\b", m):
        c["status"] = "do_not_contact"
        return f"No problem at all, sorry to bother you. All the best with {c['business']}!"
    if re.search(r"how much|price|cost|£|charge", m):
        return f"For the issues I found it's £{c['entry_price']}, done within 48 hours. Want me to go ahead?"
    if re.search(r"who (is|are)|scam|how did you get", m):
        return (f"Fair question! I'm {ME['name']}, I build and fix websites for UK small businesses. Your number's on your website, "
                f"which is how I spotted the issue. Some of my work: {ME['portfolio']}")
    if re.search(r"\b(yes|yeah|sure|ok|okay|go on|send)\b", m):
        bullets = "\n".join("• " + (hook_sentence(i) or "").split(",")[0].replace("the ", "", 1).capitalize()
                            for i in c["verified_issues"] if hook_sentence(i))[:600]
        return (f"I'm {ME['name']}, I build and fix websites for UK small businesses. Here's what I found on your site:\n{bullets}\n"
                f"I can sort all of that this week for £{c['entry_price']}. Want me to go ahead?")
    return None

def cmd_reply(k, their_msg):
    db = load_db()
    c = db[k]
    c["history"].append({"from": "them", "text": their_msg, "date": str(TODAY)})
    c["status"] = "in_conversation" if c["status"] in ("new", "contacted") else c["status"]
    system = (PLAYBOOK + f"\n\n## Sender\nName: {ME['name']}. Portfolio: {ME['portfolio']}.\n"
              "You draft the NEXT WhatsApp message only. Output just the message text, nothing else. "
              "If they opted out, output a one-line polite goodbye and start your output with [DNC].")
    facts = {"business": c["business"], "site": c["site"], "entry_price_gbp": c["entry_price"],
             "verified_issues": [f"{i['issue']}: {i['detail']}" for i in c["verified_issues"]]}
    convo = "\n".join(f"{h['from'].upper()}: {h['text']}" for h in c["history"])
    try:
        draft = llm(system, f"LEAD FACTS (only these are true):\n{json.dumps(facts, indent=1)}\n\nCONVERSATION:\n{convo}\n\nDraft my next message.")
    except Exception as e:
        print("AI call failed:", e, "- using templates")
        draft = None
    if draft and draft.startswith("[DNC]"):
        c["status"], draft = "do_not_contact", draft[5:].strip()
    draft = draft or template_reply(c, their_msg) or "(no template fits - write this one yourself)"
    save_db(db)
    print(f"\nSUGGESTED REPLY to {c['business']}:\n\n{draft}\n\nSend: {wa_link(c['number'], draft)}")
    print(f'Then log it: python dm_agent.py sent {k} "<final text>"')

def cmd_sent(k, text):
    db = load_db(); c = db[k]
    c["history"].append({"from": "me", "text": text, "date": str(TODAY)})
    if c["status"] == "new": c["status"] = "contacted"
    save_db(db); print("logged")

def cmd_status(k, s):
    db = load_db(); db[k]["status"] = s; save_db(db); print(k, "->", s)

def cmd_today():
    db = load_db()
    for k, c in db.items():
        if c["status"] != "contacted" or not c["history"]:
            continue
        mine = [h for h in c["history"] if h["from"] == "me"]
        last = datetime.date.fromisoformat(c["history"][-1]["date"])
        days = (TODAY - last).days
        if len(mine) == 1 and days >= 2:
            t = f"Hi again, just checking you saw my last message? Happy to send a screenshot of the issue on your site."
        elif len(mine) == 2 and days >= 3:
            t = f"Last one from me! If you ever want the website issues sorted, just message here. All the best with {c['business']}."
        else:
            continue
        print(f"\n[{k}] {c['business']} - follow-up {len(mine)} (last contact {days} days ago)\n  {t}\n  {wa_link(c['number'], t)}")

if __name__ == "__main__":
    a = sys.argv[1:]
    {"openers": lambda: cmd_openers(*a[1:]), "reply": lambda: cmd_reply(a[1], a[2]), "sent": lambda: cmd_sent(a[1], a[2]),
     "status": lambda: cmd_status(a[1], a[2]), "today": cmd_today}.get(a[0] if a else "", lambda: print(__doc__))()
