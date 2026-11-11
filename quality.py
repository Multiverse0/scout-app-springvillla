"""
Lead Quality: how likely is this lead to turn into a paying client? (0-100, grade A-D, with reasons)

No score can guarantee a sale. This ranks leads by what usually predicts one, then adjusts using YOUR results:
every reply (or silence) teaches it which niches, platforms and problems actually respond for you.
"""
import re

NICHES = [  # (name, keywords, value 0-10: how much a website matters to them + what they can pay)
    ("cleaning", r"clean|valet|pressure wash|carpet|oven clean|window clean|janitorial|end of tenancy", 8),
    ("trades", r"plumb|electric|roof|builder|building|construction|joiner|carpent|plaster|tiler|decorat|painter|handyman|gas|boiler|heating|"
               r"locksmith|glaz|window|kitchen|bathroom|landscap|garden|fencing|driveway|paving|scaffold|groundwork|loft|extension|damp", 9),
    ("health", r"clinic|dental|dentist|physio|chiropract|osteopath|aesthetic|botox|massage|therap|counsel|optician|podiat|nutrition|pharmacy", 9),
    ("beauty", r"salon|barber|hair|nail|lash|brow|beauty|spa|tattoo|makeup|wax", 7),
    ("auto", r"mechanic|garage|mot\b|car repair|tyre|detailing|bodyshop|car wash|mobile mechanic|recovery", 8),
    ("property", r"estate agent|letting|property management|surveyor|removal|storage|conveyanc", 8),
    ("professional", r"accountant|bookkeep|solicitor|law firm|legal|consultan|architect|insurance|mortgage|financial advis|recruit", 8),
    ("manufacturing", r"fabricat|engineering|machining|cnc|welding|manufactur|packaging|signage|signs|printing|pallet|moulding|metal", 9),
    ("food", r"restaurant|cafe|bakery|catering|takeaway|food truck|butcher|deli|cake", 6),
    ("fitness", r"gym|personal train|fitness|yoga|pilates|martial arts|boxing|dance school", 6),
    ("education", r"tutor|tuition|driving school|driving instructor|nursery|childcare|lessons", 6),
    ("pets", r"dog groom|pet|vet|kennel|cattery|dog walk", 6),
    ("events", r"wedding|photograph|videograph|dj\b|venue|florist|event", 6),
    ("retail", r"shop|boutique|store|jewell|fashion|clothing", 5),
]
HIGH_PAIN = {"broken_whatsapp_button": 16, "fake_phone_number": 16, "broken_call_button": 14, "fake_email": 14,
             "whatsapp_button_on_landline": 10, "no_quote_option": 12, "not_mobile_friendly": 12, "site_down": 6,
             "placeholder_text": 10, "dead_button": 6, "empty_social_link": 5, "no_https": 7, "dated_build": 7,
             "slow_site": 5, "no_contact_form": 4, "no_whatsapp_button": 3, "old_copyright": 3, "broken_images": 4,
             "weak_google_listing": 2, "no_custom_domain": 3}

def niche_of(lead):
    text = " ".join([lead.get("business", ""), lead.get("scan", {}).get("title", ""), (lead.get("company") or {}).get("about", ""),
                     (lead.get("company") or {}).get("sector", ""), lead.get("hook_text") or "", lead.get("scan", {}).get("snippet", "")]).lower()
    for name, pat, val in NICHES:
        if re.search(pat, text): return name, val
    return "other", 4

def _learned(d):
    """Reply rates from your own outreach, by niche / platform / main problem (smoothed so a few results don't swing it)."""
    stats, total, replied = {}, 0, 0
    for l in d["leads"].values():
        if not any(h["from"] == "me" for h in l.get("history", [])): continue
        r = any(h["from"] == "them" for h in l["history"]) and l.get("status") != "do_not_contact"
        total += 1; replied += r
        for key in _keys(l):
            s = stats.setdefault(key, [0, 0]); s[0] += 1; s[1] += r
    base = (replied + 1) / (total + 2) if total else 0.3
    return stats, base, total

def _keys(l):
    import platforms
    issues = sorted(l.get("scan", {}).get("issues", []), key=lambda i: -HIGH_PAIN.get(i["issue"], 0))
    return [f"niche:{niche_of(l)[0]}", f"platform:{platforms.platform_of(l.get('scan', {}).get('site', '')) or ('company' if l.get('source') == 'company' else 'other')}",
            f"problem:{issues[0]['issue'] if issues else 'none'}"]

def score(l, d, learned=None):
    """Returns {"score", "grade", "reasons": [(+/-points, text)], "niche"}."""
    s = l.get("scan", {}); sig = s.get("signals", {}); reasons = []
    def add(pts, why):
        reasons.append((pts, why))
    # hard stops
    if s.get("offline"): return {"score": 0, "grade": "D", "reasons": [(-100, "site is offline / deleted")], "niche": "other"}
    if s.get("competitor"): return {"score": 5, "grade": "D", "reasons": [(-95, "web designer / agency (a competitor)")], "niche": "other"}
    if not s.get("is_uk"): return {"score": 5, "grade": "D", "reasons": [(-95, "not a UK business")], "niche": "other"}
    # 1. can we reach a real person?
    company = l.get("source") == "company"        # B2B firms: a business email + office line is the normal, good route
    if l.get("number"): add(22, "UK mobile (WhatsApp) for the owner")
    if l.get("email"):
        e = l["email"].lower(); free = e.split("@")[-1] in ("gmail.com", "hotmail.com", "outlook.com", "yahoo.co.uk", "yahoo.com", "icloud.com", "hotmail.co.uk", "live.co.uk")
        add((18 if company else 10) if not free else 7, "verified email" + (" (owner's personal inbox)" if free else " at their own domain" if company else ""))
    if company and not l.get("number") and any(p.get("kind") == "landline" for p in s.get("phones", [])): add(5, "office phone listed")
    if not l.get("number") and not l.get("email"): add(-40, "no verified way to contact them")
    # 2. pain that costs them money
    pains = sorted({i["issue"] for i in s.get("issues", [])}, key=lambda k: -HIGH_PAIN.get(k, 0))
    pain_pts = min(30, sum(HIGH_PAIN.get(k, 1) for k in pains[:3]))
    if pains: add(pain_pts, "problems that lose them enquiries: " + ", ".join(pains[:3]).replace("_", " "))
    else: add(-10, "website has no clear problems")
    # 3. a real, active business that can pay
    real = [("address", 6, "has a business address"), ("hours", 4, "lists opening hours"), ("prices", 4, "shows prices"),
            ("reviews", 6, "shows reviews"), ("registered", 4, "registered company"), ("established", 4, "established business"),
            ("insured", 4, "accredited / insured"), ("booking", 3, "takes bookings or quote requests")]
    pts = [p for k, p, _ in real if sig.get(k)]
    if pts: add(min(20, sum(pts)), "real active business (" + ", ".join(w for k, _, w in real if sig.get(k))[:120] + ")")
    if sig and sig.get("words", 0) < 120: add(-8, "very little on the site, may not be a running business")
    size = (l.get("company") or {}).get("size", "")
    if size in ("10-19", "20-49", "50-99", "100-199"): add(6, f"{size} staff: budget for a proper website")
    # 4. built it themselves (needs help) vs already has a web person
    if l.get("source") != "company" and s.get("platform"): add(6, "DIY site on a free builder address")
    if any(i["issue"] == "placeholder_text" for i in s.get("issues", [])): add(4, "template text left in: unfinished DIY build")
    if sig.get("agency_credit"): add(-25, "footer credits a web designer: they already have someone")
    if sig.get("non_commercial"): add(-35, "looks like a club, charity, student or demo project")
    if sig.get("franchise"): add(-15, "chain / franchise: head office controls the website")
    # 5. niche value
    niche, val = niche_of(l)
    add((val - 5) * 2, f"niche: {niche}")
    raw = sum(p for p, _ in reasons)
    # 6. what YOUR results say (needs a few contacted leads to kick in)
    stats, base, total = learned or _learned(d)
    if total >= 8:
        mult = 1.0
        for key in _keys(l):
            n, r = stats.get(key, [0, 0])
            if n >= 3:
                rate = (r + 1) / (n + 2)
                f = max(0.75, min(1.35, rate / base)); mult *= f
                if abs(f - 1) >= 0.1:
                    reasons.append((0, f"your results: {key.split(':')[1].replace('_', ' ')} gets {r}/{n} replies ({'above' if f > 1 else 'below'} your average)"))
        raw = raw * max(0.6, min(1.6, mult))
    sc = max(0, min(100, round(raw)))
    grade = "A" if sc >= 80 else "B" if sc >= 65 else "C" if sc >= 50 else "D"
    return {"score": sc, "grade": grade, "reasons": sorted(reasons, key=lambda x: -abs(x[0])), "niche": niche}

def score_all(d):
    learned = _learned(d)
    return {k: score(l, d, learned) for k, l in d["leads"].items()}

def report(d):
    """Your outreach results so far, by niche / platform / problem (for the dashboard)."""
    stats, base, total = _learned(d)
    rows = [{"key": k.split(":", 1)[1].replace("_", " "), "kind": k.split(":")[0], "contacted": n, "replied": r,
             "rate": round(r / n * 100) if n else 0} for k, (n, r) in stats.items() if n >= 2]
    return {"total": total, "base": round(base * 100), "rows": sorted(rows, key=lambda x: (-x["contacted"], -x["rate"]))}
