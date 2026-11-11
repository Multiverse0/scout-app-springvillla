"""
Base44 Scout v1 - finds contacts on unfinished Base44 sites and verifies each one.

Usage:
    python scout.py urls.txt              # one site URL per line
    python scout.py urls.txt --pages 4    # also crawl up to 4 internal pages (contact, about...)

Output: leads.csv + leads.json (every contact keeps its evidence: page, how it was found, time checked)

Rule: nothing is reported as a contact unless it passes verification.
Anything fake/placeholder is moved into "issues" (which becomes a pitch point instead).
"""
import argparse, csv, json, re, sys, datetime
from urllib.parse import urlparse, urljoin, unquote

import phonenumbers
from phonenumbers import PhoneNumberType, number_type
import urllib.request
from playwright.sync_api import sync_playwright

NOW = lambda: datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------- authenticity checks ----------

def is_dummy_digits(national: str) -> bool:
    d = re.sub(r"\D", "", national)
    if len(d) < 9:
        return True
    tail = d[-7:]
    if len(set(tail)) <= 2:                       # 7000000, 8000000, 1111111
        return True
    if tail in "01234567890" or tail in "9876543210":   # 1234567 style runs
        return True
    if re.search(r"123456|234567|345678|456789|987654|876543|654321", d[-8:]):  # 07425 123456 style
        return True
    if re.match(r"^7700900\d{3}$", d[-10:]):      # Ofcom TV/drama range 07700 900xxx
        return True
    if re.match(r"^(20|113|114|115|116|117|118|121|131|141|151|161|191)79460\d{2,3}$", d[-10:]):  # Ofcom drama landlines
        return True
    return False

def check_phone(raw: str):
    """Return dict with verified info, or reason it failed."""
    try:
        n = phonenumbers.parse(raw, "GB")
    except phonenumbers.NumberParseException:
        return {"ok": False, "reason": "unparseable"}
    e164 = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
    if not phonenumbers.is_valid_number(n):
        return {"ok": False, "e164": e164, "reason": "invalid number"}
    if is_dummy_digits(e164):
        return {"ok": False, "e164": e164, "reason": "placeholder/fake number"}
    t = number_type(n)
    kind = {PhoneNumberType.MOBILE: "mobile", PhoneNumberType.FIXED_LINE: "landline",
            PhoneNumberType.FIXED_LINE_OR_MOBILE: "landline_or_mobile"}.get(t, "other")
    return {"ok": True, "e164": e164, "country": phonenumbers.region_code_for_number(n), "kind": kind}

FAKE_EMAIL_DOMAINS = {"example.com", "example.co.uk", "email.com", "domain.com", "yourdomain.com",
                      "yourbusiness.com", "company.com", "test.com", "sentry.io", "base44.com",
                      "lovable.dev", "replit.com", "vercel.com", "netlify.com", "bolt.new", "emergent.sh"}
_mx_cache = {}

def mx_lookup(dom):
    """True = has mail server, False = none, None = couldn't check. Uses DNS-over-HTTPS (works behind proxies)."""
    try:
        r = json.load(urllib.request.urlopen(f"https://dns.google/resolve?name={dom}&type=MX", timeout=10))
        return bool(r.get("Answer")) and r.get("Status") == 0
    except Exception:
        return None

def check_email(addr: str):
    addr = addr.strip().lower().rstrip(".")
    addr = re.sub(r"^\d{4,}(?=[a-z])", "", addr)      # "01234 567890sales@x.co.uk" glued together in page text
    if not re.match(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$", addr):
        return {"ok": False, "reason": "bad format"}
    dom = addr.split("@")[1]
    if dom in FAKE_EMAIL_DOMAINS or addr.split("@")[0] in {"you", "your", "name", "email", "user"}:
        return {"ok": False, "reason": "placeholder email"}
    if dom not in _mx_cache:
        _mx_cache[dom] = mx_lookup(dom)
    if _mx_cache[dom] is None:
        return {"ok": True, "email": addr, "domain": dom, "mx": "unverified (lookup failed)"}
    if not _mx_cache[dom]:
        return {"ok": False, "reason": f"{dom} has no mail server - emails to it bounce"}
    return {"ok": True, "email": addr, "domain": dom}

SOCIAL_HOSTS = {"instagram.com": "instagram", "facebook.com": "facebook", "fb.com": "facebook",
                "tiktok.com": "tiktok", "linkedin.com": "linkedin", "twitter.com": "x", "x.com": "x",
                "youtube.com": "youtube"}

def check_social(url: str):
    p = urlparse(url)
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("uk.")
    net = SOCIAL_HOSTS.get(host)
    if not net:
        return None
    path = p.path.strip("/")
    if net == "facebook" and "profile.php" in path and "id=" in p.query:
        path = "profile.php?" + p.query
    if not path or path.lower() in {"home", "explore", "share", "sharer", "intent", "login"}:
        return {"ok": False, "network": net, "url": url, "reason": "links to bare homepage, not a profile"}
    return {"ok": True, "network": net, "url": url, "handle": path}

UK_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[A-Z]{2})\b")
PHONE_TEXT = re.compile(r"(?:\+44\s?\(?0?\)?\s?|\b0)(?:7\d{3}|20|1\d{2,3}|3\d{2})[\s-]?\d{3,4}[\s-]?\d{3,4}\b")
EMAIL_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PLACEHOLDER_TEXT = re.compile(r"lorem ipsum|your business name|your company|coming soon|placeholder|"
                              r"insert (text|image)|add your|123 main st|john doe|jane doe|\[.*?name\]", re.I)

# ---------- page extraction ----------

JS_EXTRACT = """() => ({
  links: [...document.querySelectorAll('a[href]')].map(a => ({href: a.href, text: (a.innerText||'').trim().slice(0,60)})),
  text: document.body ? document.body.innerText : '',
  title: document.title
})"""

def scan_site(page, start_url, max_pages):
    host = urlparse(start_url).netloc
    queue, seen, pages = [start_url], set(), []
    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            # don't wait for the page to go "quiet" (chat widgets/analytics never do): wait until real content shows
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_function("() => document.body && (document.body.innerText.length > 400 || "
                                       "document.querySelector('a[href^=\"tel:\"],a[href^=\"mailto:\"],a[href*=\"wa.me\"]'))", timeout=7000)
            except Exception:
                pass
            page.wait_for_timeout(700)
            data = page.evaluate(JS_EXTRACT)
        except Exception as e:
            pages.append({"url": url, "error": str(e)[:120]})
            continue
        data["url"] = url
        pages.append(data)
        # prioritise contact/about pages
        for l in data["links"]:
            h = l["href"].split("#")[0]
            if urlparse(h).netloc == host and h not in seen and h not in queue:
                if re.search(r"contact|about|book|quote|get-in-touch", h, re.I):
                    queue.insert(0, h)
                else:
                    queue.append(h)
    return pages

def analyse(start_url, pages):
    lead = {"site": start_url, "title": "", "checked_at": NOW(),
            "whatsapp": [], "phones": [], "emails": [], "socials": [],
            "uk_signals": [], "issues": [], "pages_scanned": []}
    phones, emails, socials = {}, {}, {}

    def add_issue(kind, detail, page):
        item = {"issue": kind, "detail": detail, "page": page}
        if not any(i["issue"] == kind and i["detail"] == detail for i in lead["issues"]):
            lead["issues"].append(item)

    for pg in pages:
        u = pg["url"]
        lead["pages_scanned"].append(u)
        if "error" in pg:
            add_issue("page_failed_to_load", pg["error"], u)
            continue
        lead["title"] = lead["title"] or pg["title"]
        text = pg["text"]

        for l in pg["links"]:
            h = l["href"]
            # WhatsApp buttons
            m = re.search(r"(?:wa\.me/|whatsapp\.com/send\?phone=|api\.whatsapp\.com/send\?phone=)\+?(\d+)", h)
            if m:
                r = check_phone("+" + m.group(1))
                if not r["ok"]:
                    add_issue("broken_whatsapp_button", f"{h} -> {r['reason']}", u)
                elif r["kind"] == "landline":
                    add_issue("whatsapp_button_on_landline", f"{r['e164']} is a landline - most can't receive WhatsApp", u)
                    phones.setdefault(r["e164"], {**r, "evidence": []})["evidence"].append({"page": u, "found_in": "whatsapp button"})
                else:
                    e = phones.setdefault(r["e164"], {**r, "evidence": []})
                    e["evidence"].append({"page": u, "found_in": "whatsapp button"})
                    e["whatsapp_button"] = True
                continue
            if h.startswith("tel:"):
                num = unquote(h[4:]).strip()
                if len(re.sub(r"\D", "", num)) < 6:
                    add_issue("broken_call_button", f"call button has no number ({h})", u)
                    continue
                r = check_phone(num)
                if r["ok"]:
                    phones.setdefault(r["e164"], {**r, "evidence": []})["evidence"].append({"page": u, "found_in": "call button"})
                else:
                    if r["reason"] == "placeholder/fake number":
                        add_issue("fake_phone_number", f"{h} -> {r['reason']}", u)
                continue
            if h.startswith("mailto:"):
                addr = unquote(h[7:].split("?")[0])
                r = check_email(addr)
                if r["ok"]:
                    emails.setdefault(r["email"], {**r, "evidence": []})["evidence"].append({"page": u, "found_in": "email link"})
                else:
                    add_issue("fake_email", f"{addr} -> {r['reason']}", u)
                continue
            s = check_social(h)
            if s:
                if s["ok"]:
                    socials.setdefault(s["url"].rstrip("/"), {**s, "evidence": []})["evidence"].append({"page": u, "found_in": "link"})
                else:
                    add_issue("empty_social_link", f"{s['network']} icon goes to {h}", u)
                continue
            if (h.endswith("#") or h.startswith("javascript:")) and re.search(r"[A-Za-z]{3}", l["text"]) \
                    and re.search(r"book|call|contact|quote|whatsapp|message|enquir|order|buy|shop|learn|view|see|get", l["text"], re.I):
                add_issue("dead_button", f"'{l['text']}' goes nowhere", u)

        # numbers / emails written in text
        for raw in set(PHONE_TEXT.findall(text)):
            r = check_phone(raw)
            if r["ok"]:
                phones.setdefault(r["e164"], {**r, "evidence": []})["evidence"].append({"page": u, "found_in": f"page text '{raw.strip()}'"})
            else:
                if r["reason"] == "placeholder/fake number":
                    add_issue("fake_phone_number", f"'{raw.strip()}' -> {r['reason']}", u)
        for addr in set(EMAIL_TEXT.findall(text)):
            r = check_email(addr)
            if r["ok"]:
                emails.setdefault(r["email"], {**r, "evidence": []})["evidence"].append({"page": u, "found_in": "page text"})
            else:
                add_issue("fake_email", f"{addr} -> {r['reason']}", u)

        for m in {x.group(0) for x in PLACEHOLDER_TEXT.finditer(text)}:
            add_issue("placeholder_text", f"'{m}' still on page", u)
        for pc in set(UK_POSTCODE.findall(text)):
            sig = f"postcode {pc[0]} {pc[1]}"
            if sig not in lead["uk_signals"]:
                lead["uk_signals"].append(sig)
        if "£" in text and "£ prices" not in lead["uk_signals"]:
            lead["uk_signals"].append("£ prices")
        lead.setdefault("_text", []).append(text[:20000])

    # deleted / never-deployed sites (common on Netlify, Vercel, Replit): not a lead
    first = next((pg for pg in pages if "error" not in pg), None)
    dead_re = re.compile(r"site not found|page not found|deployment not found|project not found|404|not found|no such app|"
                         r"application error|this deployment|isn.t available|has been (paused|suspended)|repl.*(sleeping|not found)", re.I)
    if not first or (dead_re.search(first.get("title", "")) or (len(first.get("text", "")) < 400 and dead_re.search(first.get("text", "")))):
        lead["offline"] = True
        add_issue("site_offline", "the site is offline or deleted (shows an error page)", start_url)
    import platforms
    _plat = platforms.platform_of(start_url)
    if _plat:
        add_issue("no_custom_domain", f"still on a free .{_plat} address", start_url)
    lead["platform"] = _plat

    for p in phones.values():
        p["sources"] = len({e["page"] + e["found_in"] for e in p["evidence"]})
        p["confidence"] = "high" if p["sources"] >= 2 else "medium"
        if p["country"] == "GB":
            lead["uk_signals"].append(f"UK phone {p['e164']}") if f"UK phone {p['e164']}" not in lead["uk_signals"] else None
    lead["phones"] = sorted(phones.values(), key=lambda x: -x["sources"])
    lead["emails"] = list(emails.values())
    lead["socials"] = list(socials.values())

    # WhatsApp target: a verified mobile (button first, then any mobile)
    mobiles = [p for p in lead["phones"] if p["kind"] in ("mobile", "landline_or_mobile")]
    mobiles.sort(key=lambda p: (not p.get("whatsapp_button"), -p["sources"]))
    lead["whatsapp"] = [{"number": p["e164"], "wa_link": "https://wa.me/" + p["e164"].lstrip("+"),
                         "basis": "site has WhatsApp button" if p.get("whatsapp_button") else "UK mobile - WhatsApp likely, confirm when you open link",
                         "confidence": p["confidence"]} for p in mobiles]
    if not any(p.get("whatsapp_button") for p in lead["phones"]) and \
            not any(i["issue"] in ("broken_whatsapp_button", "whatsapp_button_on_landline") for i in lead["issues"]):
        add_issue("no_whatsapp_button", "customers can't message them on WhatsApp from the site", start_url)

    # UK or not? A UK number alone isn't proof (diaspora businesses abroad often list one), so weigh both sides.
    alltext = " ".join(lead.pop("_text", []))
    foreign = []
    for p_ in lead["phones"]:
        if p_.get("country") and p_["country"] != "GB": foreign.append(f"{p_['country']} phone")
    for w_ in re.findall(r"\b(Nigeria|Lagos|Abuja|Ghana|Accra|Kenya|Nairobi|South Africa|Johannesburg|India|Mumbai|Delhi|Pakistan|"
                         r"United States|USA|Florida|Texas|California|New York|Canada|Toronto|Australia|Sydney|Dubai|UAE|"
                         r"Ireland|Dublin|Morocco|Philippines|Malaysia|Singapore)\b", alltext):
        foreign.append(w_)
    if re.search(r"\$\s?\d", alltext) and "£" not in alltext: foreign.append("$ prices")
    if re.search(r"\b[A-Z]{2}\s\d{5}(-\d{4})?\b", alltext): foreign.append("US zip code")
    foreign = list(dict.fromkeys(foreign))
    uk_score = 2 * sum(s_.startswith("postcode") for s_ in lead["uk_signals"]) + sum(s_.startswith("UK phone") for s_ in lead["uk_signals"]) \
               + (1 if "£ prices" in lead["uk_signals"] else 0) + len(re.findall(r"\b(United Kingdom|England|Scotland|Wales|London)\b", alltext)[:3])
    lead["foreign_signals"] = foreign
    lead["is_uk"] = any(s_.startswith(("UK phone", "postcode")) for s_ in lead["uk_signals"]) and uk_score > len(foreign)
    # business signals used by the Lead Quality score (quality.py)
    T = alltext; t = T.lower()
    lead["signals"] = {
        "reviews": bool(re.search(r"\b(reviews?|testimonials?|what our (customers|clients) say|★|5 stars?|rated \d(\.\d)? out of|trustpilot|google reviews|checkatrade)\b", t)),
        "hours": bool(re.search(r"\b(opening hours|open (mon|tue|7 days)|mon(day)?\s*[-–to]+\s*(fri|sat|sun)|9(am|:00)\s*[-–]\s*[5-6](pm|:00)|appointments? available)\b", t)),
        "prices": "£" in T,
        "address": any(s_.startswith("postcode") for s_ in lead["uk_signals"]),
        "registered": bool(re.search(r"\b(ltd|limited|company (no|number|reg)|registered in england|vat (no|number|reg))\b", t)),
        "established": bool(re.search(r"\b(since|established|est\.?|founded( in)?)\s*(in\s*)?(19|20)\d{2}\b|\b\d{1,2}\+? years'? (of )?experience\b", t)),
        "insured": bool(re.search(r"\b(fully insured|public liability|accredited|certified|gas safe|niceic|fca|cqc|dbs[- ]checked|city & guilds)\b", t)),
        "booking": bool(re.search(r"\b(book (now|online|an appointment)|get a (free )?quote|request a quote|free estimate|call us|callout)\b", t)),
        "agency_credit": bool(re.search(r"\b(website|site|web design)\s+(by|designed by|created by|built by)\s+(?!base44|lovable|replit|wix|squarespace|godaddy|wordpress|webflow|framer|bolt|emergent)[a-z0-9]", t)),
        "non_commercial": bool(re.search(r"\b(registered charity|charity|church|ministry|mosque|parish|football club|sports club|f\.?c\.?|"
                                         r"school project|coursework|assignment|hackathon|test site|pageant|fan club|student project|demo site)\b",
                                         ((lead.get("title") or "") + " " + T[:300]).lower())),
        "franchise": bool(re.search(r"\b(franchise|nationwide network|branches across|find your nearest (branch|store))\b", t)),
        "words": len(T.split()),
    }
    # web designers / agencies are competitors, not clients
    # web designers / agencies are competitors, not clients: judge by what the business calls ITSELF (title / first lines),
    # not by words like "portfolio" (trades often show a portfolio of their work)
    ident = ((lead.get("title") or "") + " " + alltext[:400]).lower()
    lead["competitor"] = bool(re.search(r"\b(web ?design(er|ers|s)?|web ?development|web ?studio|website design|digital (marketing )?agency|"
                                        r"creative agency|seo (agency|services|consultant)|app development|we (design|build) websites|"
                                        r"websites? for (small )?businesses)\b", ident))
    lead["snippet"] = re.sub(r"\s+", " ", alltext[:600])
    weights = {"broken_whatsapp_button": 3, "fake_phone_number": 3, "fake_email": 2, "broken_call_button": 3, "placeholder_text": 3,
               "empty_social_link": 2, "dead_button": 1, "whatsapp_button_on_landline": 2,
               "no_whatsapp_button": 1, "no_custom_domain": 1, "page_failed_to_load": 1}
    lead["unfinished_score"] = sum(weights.get(i["issue"], 1) for i in lead["issues"])
    lead["status"] = ("ready_whatsapp" if lead["whatsapp"] else
                      "needs_number_lookup" if (lead["emails"] or lead["socials"]) else "no_verified_contact")
    return lead

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls_file")
    ap.add_argument("--pages", type=int, default=4)
    ap.add_argument("--out", default="leads")
    a = ap.parse_args()
    urls = [l.strip() for l in open(a.urls_file) if l.strip() and not l.startswith("#")]
    urls = list(dict.fromkeys("https://" + urlparse(u if "//" in u else "https://" + u).netloc for u in urls))
    leads = []
    with sync_playwright() as p:
        br = p.chromium.launch()
        page = br.new_page(user_agent="Mozilla/5.0 (compatible; LeadScout/1.0)")
        for u in urls:
            print("scanning", u, file=sys.stderr)
            leads.append(analyse(u, scan_site(page, u, a.pages)))
        br.close()
    leads.sort(key=lambda l: (l["status"] != "ready_whatsapp", -l["unfinished_score"]))
    json.dump(leads, open(a.out + ".json", "w"), indent=2)
    with open(a.out + ".csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site", "business", "uk", "status", "unfinished_score", "whatsapp", "whatsapp_basis",
                    "other_phones", "emails", "socials", "issues (pitch points)", "checked_at"])
        for l in leads:
            w.writerow([l["site"], l["title"], l["is_uk"], l["status"], l["unfinished_score"],
                        " | ".join(x["wa_link"] for x in l["whatsapp"]),
                        " | ".join(x["basis"] for x in l["whatsapp"]),
                        " | ".join(f"{p['e164']} ({p['kind']})" for p in l["phones"] if p["kind"] == "landline"),
                        " | ".join(e["email"] for e in l["emails"]),
                        " | ".join(s["url"] for s in l["socials"]),
                        " | ".join(f"{i['issue']}: {i['detail']}" for i in l["issues"]),
                        l["checked_at"]])
    print(f"done: {len(leads)} sites -> {a.out}.csv / {a.out}.json", file=sys.stderr)

if __name__ == "__main__":
    main()
