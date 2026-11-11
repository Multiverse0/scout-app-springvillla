"""
Outreach engine: email sending (SMTP), reply/bounce detection (IMAP), and follow-up sequences
for email + WhatsApp. Used by app.py, which runs tick() every few minutes in the background.

Rules built in (not editable from templates):
  * every email ends with an opt-out line; "unsubscribe/stop/remove" replies -> do-not-contact + suppression list
  * a reply on ANY channel stops ALL sequences for that lead
  * bounces stop email for that lead
  * emails only go out in the sending window (UK time) and under the daily cap, spaced out randomly
"""
import stopper
import smtplib, imaplib, email, email.utils, re, random, time, datetime, ssl, html
from email.message import EmailMessage
from collections import defaultdict
from zoneinfo import ZoneInfo

import dm_agent

UK = ZoneInfo("Europe/London")
def uk_now(): return datetime.datetime.now(UK)
def iso(dt): return dt.isoformat(timespec="seconds")
def parse(s): return datetime.datetime.fromisoformat(s)

OPT_OUT = 'Not relevant? Just reply "unsubscribe" and I won\'t contact you again.'
UNSUB_RE = re.compile(r"\b(unsubscribe|stop|remove me|not interested|no thanks|don'?t (contact|email|message))\b", re.I)
BAD_EMAIL_PREFIX = ("careers", "career", "jobs", "recruitment", "hr", "accounts", "invoice", "billing", "noreply",
                    "no-reply", "privacy", "legal", "dpo", "abuse", "webmaster", "postmaster")
FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "live.co.uk",
             "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "aol.com", "btinternet.com", "sky.com"}

DEFAULT_EMAIL_SETTINGS = {
    "mode": "paused",            # paused | live
    "from_name": "", "from_email": "",
    "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_user": "", "smtp_pass": "",
    "imap_host": "imap.gmail.com", "imap_port": 993,
    "daily_cap": 30, "start_hour": 9, "end_hour": 17, "weekdays_only": True,
    "gap_min_s": 60, "gap_max_s": 180, "email_delay_days_if_whatsapp": 1,
}

DEFAULT_SEQUENCES = {
    "email": [
        {"day": 0, "subject": "Quick one about the {business} website",
         "body": "Hi,\n\nI was looking at the {business} website ({site_short}) and noticed {hook}\n\n"
                 "I've taken a screenshot of it, along with a couple of other small things I spotted. Want me to send them over?\n\n{signature}"},
        {"day": 3, "subject": "",
         "body": "Hi again,\n\nJust floating this back up. Here's what I found on {site_short}:\n{issues_bullets}\n\n"
                 "I can sort all of that this week for £{price}. Worth a quick reply?\n\n{signature}"},
        {"day": 7, "subject": "",
         "body": "Hi,\n\nOne more idea: I can put together a free before/after mockup of your homepage so you can see "
                 "what it could look like finished. No obligation. Want one?\n\n{signature}"},
        {"day": 12, "subject": "",
         "body": "Hi,\n\nI'll leave it here so I don't clutter your inbox. If you ever want the website sorted, "
                 "just reply to this email.\n\nAll the best with {business},\n{name}"},
    ],
    "whatsapp": [
        {"day": 0, "body": "{opener}"},
        {"day": 2, "body": "Hi again, just checking you saw my last message? Happy to send a screenshot of the issue on your site."},
        {"day": 5, "body": "Last one from me! If you ever want the website issues sorted, just message here. All the best with {business}."},
    ],
}

# ---------------- choosing contacts ----------------
def best_email(lead):
    cands = [e["email"] for e in lead["scan"].get("emails", [])
             if not e["email"].split("@")[0].startswith(BAD_EMAIL_PREFIX)]
    pref = ("hello", "info", "contact", "office", "enquiries", "bookings", "admin")
    cands.sort(key=lambda a: (a.split("@")[0] not in pref, a))
    return cands[0] if cands else None

def can_email(lead, d):
    e = lead.get("email")
    return bool(e and lead["scan"].get("is_uk") and e not in d.get("suppression", [])
                and lead.get("email_status") != "bounced" and lead.get("status") != "do_not_contact")

def can_whatsapp(lead, d):
    n = lead.get("number")
    return bool(n and lead["scan"].get("is_uk") and n not in d.get("suppression", [])
                and lead.get("status") != "do_not_contact")

# ---------------- templating ----------------
def seqs_for(d, lead):
    """Company leads use their own message set; everything else uses the main (Base44) sequences."""
    return d.get("sequence_sets", {}).get(lead.get("sequence_set") or "") or d.get("sequences", DEFAULT_SEQUENCES)

def hook_text(lead):
    if lead.get("hook_text"): return lead["hook_text"]
    issues = sorted([i for i in lead["scan"]["issues"] if i["issue"] in dm_agent.PRIORITY],
                    key=lambda i: dm_agent.PRIORITY.index(i["issue"]))
    if issues:
        return dm_agent.hook_sentence(issues[0])
    import platforms
    dom = platforms.platform_of(lead["scan"]["site"]) or "builder"
    return f"it's still on a free .{dom} address, which makes it harder for customers to find you on Google and trust the site."

def issues_bullets(lead):
    if lead.get("bullets"): return lead["bullets"]
    out = []
    for i in lead["scan"]["issues"]:
        s = dm_agent.hook_sentence(i) if i["issue"] in dm_agent.PRIORITY else (
            f"the site is still on a free web address ({i['detail'].split('free ')[-1].replace(' address', '')})" if i["issue"] == "no_custom_domain" else None)
        if s:
            s = s.split(", so ")[0].rstrip(".")
            out.append("- " + s[0].upper() + s[1:])
    return "\n".join(dict.fromkeys(out)) or "- A few small fixes"

def variables(lead, settings):
    es = settings.get("email", {})
    name = settings.get("name") or es.get("from_name") or "YOUR_NAME"
    sig = name + ("\n" + settings["portfolio"] if settings.get("portfolio") else "")
    return defaultdict(str, {
        "business": lead["business"], "site": lead["scan"]["site"], "platform": __import__("platforms").label_for(lead["scan"]["site"]),
        "site_short": lead["scan"]["site"].replace("https://", ""),
        "hook": hook_text(lead), "issues_bullets": issues_bullets(lead), "price": lead.get("entry_price", 50),
        "name": name, "portfolio": settings.get("portfolio", ""), "signature": sig, "opener": lead.get("opener") or "",
    })

def render(tpl, lead, settings):
    return tpl.format_map(variables(lead, settings)).strip()

def render_email_step(lead, settings, seqs, step):
    st = seqs["email"][step]
    subj = render(st.get("subject") or "", lead, settings)
    first_subj = render(seqs["email"][0]["subject"], lead, settings)
    if not subj:
        subj = "Re: " + (lead.get("seq", {}).get("email", {}).get("subject") or first_subj)
    body = render(st["body"], lead, settings) + "\n\n--\n" + OPT_OUT
    return subj, body

# ---------------- sequences ----------------
def start(lead, channel, d, delay_days=0):
    lead.setdefault("seq", {})
    lead["seq"][channel] = {"state": "active", "step": 0, "started": iso(uk_now()),
                            "next_at": iso(uk_now() + datetime.timedelta(days=delay_days)), "msgids": [], "subject": ""}

def advance(lead, channel, seqs, sent_at=None):
    s = lead["seq"][channel]; steps = seqs[channel]
    sent_at = sent_at or uk_now()
    s["last_sent"] = iso(sent_at)
    s["step"] += 1
    if s["step"] >= len(steps):
        s["state"], s["next_at"] = "done", None
    else:
        gap = steps[s["step"]]["day"] - steps[s["step"] - 1]["day"]
        s["next_at"] = iso(sent_at + datetime.timedelta(days=max(gap, 1)))

def stop_all(lead, reason):
    for s in lead.get("seq", {}).values():
        if s.get("state") in ("active", "paused"):
            s["state"], s["stopped_reason"], s["next_at"] = "stopped", reason, None

def in_window(es, now=None):
    now = now or uk_now()
    if es.get("weekdays_only", True) and now.weekday() >= 5:
        return False
    return int(es.get("start_hour", 9)) <= now.hour < int(es.get("end_hour", 17))

def sent_today(d):
    today = uk_now().date().isoformat()
    return sum(1 for x in d.get("sent_log", []) if x["at"][:10] == today and x.get("auto"))

# ---------------- SMTP ----------------
ON_REPLY = None   # app.py sets this: called with a lead key whenever a new email reply arrives

def smtp_send(es, to, subject, body, in_reply_to=None, references=None, attachments=None):
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((es.get("from_name") or "", es["from_email"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    mid = email.utils.make_msgid(domain=es["from_email"].split("@")[1])
    msg["Message-ID"] = mid
    msg["List-Unsubscribe"] = f"<mailto:{es['from_email']}?subject=unsubscribe>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(references or [in_reply_to])
    msg.set_content(body)
    for path in attachments or []:
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype="image", subtype="png", filename=path.replace("\\", "/").split("/")[-1])
    port = int(es.get("smtp_port", 465))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(es["smtp_host"], port, context=ctx, timeout=30) as s:
            s.login(es["smtp_user"] or es["from_email"], es["smtp_pass"]); s.send_message(msg)
    else:
        with smtplib.SMTP(es["smtp_host"], port, timeout=30) as s:
            s.starttls(context=ctx); s.login(es["smtp_user"] or es["from_email"], es["smtp_pass"]); s.send_message(msg)
    return mid

def email_ready(es):
    return all(es.get(k) for k in ("from_email", "smtp_host", "smtp_pass"))

# ---------------- IMAP: replies + bounces ----------------
def body_text(msg):
    part = None
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain" and "attachment" not in str(p.get("Content-Disposition", "")):
                part = p; break
        if not part:
            for p in msg.walk():
                if p.get_content_type() == "text/html": part = p; break
    else:
        part = msg
    if not part: return ""
    raw = part.get_payload(decode=True) or b""
    txt = raw.decode(part.get_content_charset() or "utf-8", "replace")
    if part.get_content_type() == "text/html":
        txt = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", "", txt)))
    return txt

def strip_quoted(txt):
    lines = []
    for line in txt.splitlines():
        if re.match(r"^\s*>", line) or re.match(r"^\s*On .+wrote:\s*$", line) or re.match(r"^-{2,}\s*Original Message", line, re.I) \
                or re.match(r"^\s*From:\s", line) or line.strip() == "--":
            break
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

def classify(msg, leads):
    """Return (kind, lead_key, text). kind: reply | bounce | None"""
    frm = email.utils.parseaddr(msg.get("From", ""))[1].lower()
    subj = str(msg.get("Subject", ""))
    refs = " ".join(str(msg.get(h, "")) for h in ("In-Reply-To", "References"))
    text = body_text(msg)
    if re.match(r"(mailer-daemon|postmaster)@", frm) or re.search(r"undeliverable|delivery status notification|returned mail|delivery has failed|failure notice", subj, re.I):
        for k, l in leads.items():
            if l.get("email") and l["email"].lower() in text.lower():
                return "bounce", k, subj
        return None, None, None
    for k, l in leads.items():
        ids = l.get("seq", {}).get("email", {}).get("msgids", [])
        if any(i and i in refs for i in ids):
            return "reply", k, strip_quoted(text)
    for k, l in leads.items():
        le = (l.get("email") or "").lower()
        if not le: continue
        dom = le.split("@")[1]
        if frm == le or (dom not in FREE_MAIL and frm.endswith("@" + dom) and l.get("seq", {}).get("email")):
            return "reply", k, strip_quoted(text)
    return None, None, None

_LEAD_ADDRS = set()   # filled by tick() so the inbox check only downloads emails that could matter

def fetch_inbox(es, since_days=7, wanted=None):
    """Fast inbox check: read only the headers of recent emails, then download full bodies only for
    replies from leads, replies to our emails, and bounce notices."""
    wanted = {a.lower() for a in (wanted if wanted is not None else _LEAD_ADDRS)}
    M = imaplib.IMAP4_SSL(es.get("imap_host"), int(es.get("imap_port", 993)), timeout=60)
    M.login(es.get("smtp_user") or es["from_email"], es["smtp_pass"])
    M.select("INBOX", readonly=True)
    since = (datetime.date.today() - datetime.timedelta(days=since_days)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'(SINCE "{since}")')
    nums = data[0].split()[-500:]
    out = []
    for i in range(0, len(nums), 100):
        chunk = b",".join(nums[i:i + 100])
        typ, md = M.fetch(chunk, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT IN-REPLY-TO REFERENCES MESSAGE-ID)])")
        for part in md:
            if not isinstance(part, tuple): continue
            num = part[0].split()[0]
            h = email.message_from_bytes(part[1])
            frm = email.utils.parseaddr(h.get("From", ""))[1].lower()
            dom = frm.split("@")[-1]
            interesting = (frm in wanted or any(dom == w.split("@")[-1] for w in wanted if w.split("@")[-1] not in FREE_MAIL)
                           or h.get("In-Reply-To") or re.match(r"(mailer-daemon|postmaster)@", frm)
                           or re.search(r"undeliverable|delivery status|returned mail|failure notice", str(h.get("Subject", "")), re.I))
            if interesting:
                typ, full = M.fetch(num, "(BODY.PEEK[])")
                if full and isinstance(full[0], tuple): out.append(email.message_from_bytes(full[0][1]))
    M.logout()
    return out

def process_messages(d, msgs):
    """Apply replies/bounces to leads. Returns list of events."""
    seen = set(d.setdefault("seen_msgids", []))
    events = []
    for msg in msgs:
        mid = str(msg.get("Message-ID", "")).strip()
        if not mid or mid in seen: continue
        seen.add(mid); d["seen_msgids"].append(mid)
        kind, k, text = classify(msg, d["leads"])
        if not kind: continue
        l = d["leads"][k]
        if kind == "bounce":
            l["email_status"] = "bounced"
            if l.get("seq", {}).get("email", {}).get("state") == "active":
                l["seq"]["email"].update(state="stopped", stopped_reason="bounced", next_at=None)
            events.append({"type": "bounce", "key": k, "at": iso(uk_now())})
            continue
        l["history"].append({"from": "them", "channel": "email", "text": text or "(empty reply)",
                             "at": datetime.datetime.now().isoformat(timespec="seconds"), "msgid": mid})
        l["unread"] = True
        l.setdefault("seq", {}).setdefault("email", {}).setdefault("msgids", []).append(mid)
        if UNSUB_RE.search(text or "") and len(text or "") < 300:
            l["status"] = "do_not_contact"; stop_all(l, "opted out")
            for c in (l.get("email"), l.get("number")):
                if c and c not in d.setdefault("suppression", []): d["suppression"].append(c)
        else:
            stop_all(l, "replied")
            if l["status"] in ("new", "contacted", "later"): l["status"] = "talking"
        events.append({"type": "reply", "key": k, "at": iso(uk_now())})
    d["seen_msgids"] = d["seen_msgids"][-3000:]
    return events

# ---------------- live progress (read by the dashboard every few seconds) ----------------
PROGRESS = {"active": False, "total": 0, "done": 0, "failed": 0, "current": None, "next_at": None,
            "queue": [], "results": [], "started": None, "finished": None, "stopped": None}

# ---------------- one scheduler pass ----------------
def due_emails(d):
    now = uk_now(); out = []
    for k, l in d["leads"].items():
        s = l.get("seq", {}).get("email")
        if s and s["state"] == "active" and s.get("next_at") and parse(s["next_at"]) <= now and can_email(l, d) \
                and l["status"] not in ("talking", "won", "lost", "do_not_contact"):
            out.append(k)
    out.sort(key=lambda k: d["leads"][k]["seq"]["email"]["next_at"])
    return out

def tick(load, save, lock, log):
    """Check inbox, then send due emails. Called from a background thread."""
    e0 = stopper.mark()
    with lock:
        d = load()
    es = {**DEFAULT_EMAIL_SETTINGS, **d["settings"].get("email", {})}
    status = d.setdefault("engine", {})
    if not email_ready(es):
        status["note"] = "Email not set up";
        with lock:
            d2 = load(); d2["engine"] = status; save(d2)
        return
    # 1. inbox first, so anyone who replied is stopped before we send
    _LEAD_ADDRS.clear(); _LEAD_ADDRS.update(l["email"] for l in d["leads"].values() if l.get("email") and l.get("seq", {}).get("email"))
    try:
        msgs = fetch_inbox(es)
        with lock:
            d = load(); ev = process_messages(d, msgs)
            d.setdefault("events", []).extend(ev); d["events"] = d["events"][-500:]
            d.setdefault("engine", {}).update(last_inbox=iso(uk_now()), inbox_error=None); save(d)
        for e in ev:
            log(f"{e['type']}: {e['key']}")
            if e["type"] == "reply" and ON_REPLY: ON_REPLY(e["key"])
    except Exception as e:
        with lock:
            d = load(); d.setdefault("engine", {}).update(inbox_error=str(e)[:200], last_inbox=iso(uk_now())); save(d)
    # 2. send
    if es["mode"] != "live" or not in_window(es) or stopper.stale(e0):
        return
    with lock:
        d = load(); todo = due_emails(d); left = int(es["daily_cap"]) - sent_today(d)
        batch = todo[:max(left, 0)]
        names = {k: (d["leads"][k]["business"], d["leads"][k].get("email")) for k in batch}
    if not batch: return
    PROGRESS.update(active=True, total=len(batch), done=0, failed=0, current=None, next_at=None,
                    started=iso(uk_now()), finished=None, queue=[names[k][0] for k in batch], results=[])
    try:
        for i, k in enumerate(batch):
            if not in_window(es): PROGRESS["stopped"] = "sending hours ended"; break
            if stopper.stale(e0) or load()["settings"]["email"].get("mode") != "live":
                PROGRESS["stopped"] = "stopped by you"; break
            with lock:
                d = load(); l = d["leads"][k]
                if k not in due_emails(d):
                    PROGRESS["total"] -= 1; PROGRESS["queue"] = [names[x][0] for x in batch[i + 1:]]; continue
                seqs = seqs_for(d, l)
                step = l["seq"]["email"]["step"]
                subj, body = render_email_step(l, d["settings"], seqs, step)
                prev = l["seq"]["email"].get("msgids", [])
            PROGRESS.update(current={"business": l["business"], "to": l["email"], "step": step + 1,
                                     "of": len(seqs["email"]), "subject": subj}, next_at=None,
                            queue=[names[x][0] for x in batch[i + 1:]])
            ok = False
            try:
                mid = smtp_send(es, l["email"], subj, body, in_reply_to=prev[-1] if prev else None, references=prev)
                ok = True
                with lock:
                    d = load(); l = d["leads"][k]; s = l["seq"]["email"]
                    s.setdefault("msgids", []).append(mid)
                    if step == 0: s["subject"] = subj
                    l["history"].append({"from": "me", "channel": "email", "subject": subj, "text": body,
                                         "at": datetime.datetime.now().isoformat(timespec="seconds"), "auto": True, "step": step})
                    if l["status"] == "new": l["status"] = "contacted"
                    advance(l, "email", seqs)
                    d.setdefault("sent_log", []).append({"at": iso(uk_now()), "key": k, "to": l["email"], "step": step, "auto": True})
                    d["sent_log"] = d["sent_log"][-5000:]
                    d.setdefault("engine", {})["send_error"] = None      # a successful send clears the old warning
                    save(d)
                PROGRESS["done"] += 1
                log(f"✉️ sent email {step+1} to {l['business']} ({l['email']})")
            except Exception as e:
                PROGRESS["failed"] += 1
                with lock:
                    d = load(); d["leads"][k].setdefault("errors", []).append(f"{iso(uk_now())} send failed: {str(e)[:150]}")
                    d.setdefault("engine", {})["send_error"] = str(e)[:200]; save(d)
                log(f"⚠️ email to {names[k][0]} failed: {str(e)[:80]} (will retry later)")
                if isinstance(e, smtplib.SMTPAuthenticationError):
                    PROGRESS["stopped"] = "Gmail rejected the login. Check the app password"; break
            PROGRESS["results"].insert(0, {"business": names[k][0], "to": names[k][1], "step": step + 1, "ok": ok,
                                           "at": iso(uk_now())})
            del PROGRESS["results"][20:]
            PROGRESS["current"] = None
            if i < len(batch) - 1:
                gap = random.uniform(float(es["gap_min_s"]), float(es["gap_max_s"]))
                PROGRESS["next_at"] = iso(uk_now() + datetime.timedelta(seconds=gap))
                if stopper.sleep(gap, e0): PROGRESS["stopped"] = "stopped by you"; break
    finally:
        PROGRESS.update(active=False, current=None, next_at=None, queue=[], finished=iso(uk_now()))
