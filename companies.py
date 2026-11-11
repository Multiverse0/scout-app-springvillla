"""
Companies module: UK companies from a business directory (e.g. Europages) -> their own website -> outdated-site audit.

Europages' robots.txt blocks automated tools, so Scout never crawls it. You browse it normally, copy the
results page (Ctrl+A, Ctrl+C) and paste it into Scout. Scout then:
  1. reads company names + towns from the pasted text
  2. finds each company's OWN website (search engine if available, otherwise smart domain guessing)
  3. audits that website: mobile-friendliness, HTTPS, speed, old copyright year, quote/contact options, dated code
  4. pulls verified contacts from the company's own site (same checks as Base44 leads)
Company leads use their own message sequence ("company" set) and never touch the Base44 workflow.
"""
import re, json, time, datetime, urllib.request, urllib.parse, concurrent.futures

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

SECTORS = [  # (label, Europages search term) - first one is the recommended starting niche
    ("Metal fabrication", "metal fabrication"), ("Precision engineering", "precision engineering"),
    ("CNC machining", "cnc machining"), ("Sheet metal", "sheet metal"), ("Welding", "welding"),
    ("Packaging", "packaging"), ("Signage", "signs"), ("Printing", "printing"), ("Pallets", "pallets"),
    ("Joinery & shopfitting", "joinery"), ("Plastic moulding", "plastic injection moulding"),
    ("Industrial cleaning", "industrial cleaning"), ("Electrical contractors", "electrical installation"),
    ("Building materials", "building materials"), ("Food production", "food manufacturer"),
]
def europages_link(term, country="united kingdom"):
    return f"https://www.europages.co.uk/companies/{urllib.parse.quote(country)}/{urllib.parse.quote(term)}.html"

# ---------------- 1. read pasted directory text ----------------
COUNTRY_LINE = re.compile(r"^(United Kingdom|UK|England|Scotland|Wales|Northern Ireland)\s*(?:,\s*(.+))?$", re.I)
SKIP_NAME = re.compile(r"^(filters|relevance|matching products|other products|contact supplier|view portfolio.*|get quotes|"
                       r"\d+|\d+-\d+|delivery:.*|certificates:.*|manufacturer/producer|distributor.*|service provider|"
                       r"wholesaler|agent.*|.*suppliers?|for|companies.*)$", re.I)
TYPE_RE = re.compile(r"^(Manufacturer/Producer|Distributor|Service provider|Wholesaler|Agent.*|Retailer)$", re.I)
SIZE_RE = re.compile(r"^(\d+-\d+|\d+\+|1-9|10-19|20-49|50-99|100-199|200-499|500-999|1000\+)$")

def parse_paste(text):
    """Extract companies from text copied off a directory results page."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out, seen = [], set()
    for i, line in enumerate(lines):
        m = COUNTRY_LINE.match(line)
        if not m or i == 0: continue
        name = lines[i - 1]
        if SKIP_NAME.match(name) or len(name) < 3 or len(name) > 90: continue
        key = name.lower()
        if key in seen: continue
        seen.add(key)
        if name.isupper() and len(name) > 4: name = name.title()
        c = {"name": name, "country": m.group(1), "city": (m.group(2) or "").strip().title(), "size": "", "type": "", "about": ""}
        for nxt in lines[i + 1:i + 9]:
            if COUNTRY_LINE.match(nxt): break
            if SIZE_RE.match(nxt) and not c["size"]: c["size"] = nxt
            elif TYPE_RE.match(nxt) and not c["type"]: c["type"] = nxt
            elif len(nxt) > 60 and not c["about"]: c["about"] = nxt[:300]
        out.append(c)
    # also accept a plain list: "Company name, Town" or "Company name" per line
    if not out:
        for line in lines:
            if 3 <= len(line) <= 90 and not SKIP_NAME.match(line):
                name, _, city = line.partition(",")
                if name.lower() not in seen:
                    seen.add(name.lower()); out.append({"name": name.strip(), "country": "United Kingdom", "city": city.strip().title(), "size": "", "type": "", "about": ""})
    return out

# ---------------- 2. find the company's own website ----------------
DIRECTORY_DOMAINS = ("europages", "yell.", "companieshouse", "find-and-update.company-information", "linkedin.", "facebook.",
                     "instagram.", "twitter.", "x.com", "youtube.", "endole.", "opencorporates", "bizdb", "checkatrade",
                     "trustpilot", "yelp.", "thomsonlocal", "192.com", "scoot.", "cylex", "hotfrog", "kompass", "applegate",
                     "dnb.com", "zoominfo", "glassdoor", "indeed.", "wikipedia", "google.", "bing.", "duckduckgo",
                     "amazon.", "ebay.", "alibaba", "made-in-china", "freeindex", "uk.globaldatabase", "companycheck",
                     "bloomberg", "crunchbase", "mapquest", "tripadvisor", "gov.uk", "issuu", "pinterest", "tiktok")
STOP = {"ltd", "limited", "llp", "plc", "uk", "the", "and", "co", "company", "group", "services", "service", "solutions",
        "international", "industries", "industrial", "engineering", "products", "systems", "of", "&", "gb", "europe"}

def _collapse(name):
    n = name.lower()
    n = re.sub(r"\b([a-z])\s*(?:&|\+|and)\s*([a-z])\b", r"\1\2", n)     # "A & R" / "J&E" -> "ar" / "je"
    return n.replace("&", " and ")

def name_tokens(name):
    t = re.sub(r"[^a-z0-9 ]", " ", _collapse(name))
    return [w for w in t.split() if w not in STOP and len(w) > 1]      # empty = name too generic to match safely

def domain_of(url):
    h = urllib.parse.urlparse(url if "//" in url else "https://" + url).netloc.lower().split(":")[0]
    return h.removeprefix("www.")

def is_directory(url):
    d = domain_of(url)
    return any(x in d for x in DIRECTORY_DOMAINS)

def domain_match(name, url):
    """0..1: how well the domain matches the company name."""
    d = re.sub(r"[^a-z0-9]", "", domain_of(url).split(".")[0])
    toks = name_tokens(name)
    if not toks or not d: return 0
    hit = sum(1 for t in toks if t in d)
    joined = "".join(toks)
    if joined and (joined in d or d in joined) and len(d) >= 4: return 1.0
    initials = "".join(t[0] for t in toks)
    if len(initials) >= 2 and d.startswith(initials): return 0.6
    return hit / len(toks)

def fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-GB,en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read(400_000).decode("utf-8", "replace")

def page_mentions(name, html):
    """How strongly a page is about this company (0..1). Short tokens only count as a whole-name match."""
    low = html.lower()
    words = [w for w in re.split(r"\s+", re.sub(r"[^a-z0-9& ]", " ", name.lower())) if w]
    phrase = r"\s*".join(re.escape(w) if w != "&" else r"(?:&amp;|&|and)" for w in words)
    if phrase and re.search(phrase, low): return 1.0
    toks = [t for t in name_tokens(name) if len(t) >= 4]
    if not toks: return 0
    return sum(1 for t in toks if re.search(r"\b" + re.escape(t), low)) / len(name_tokens(name))

def search_urls(settings, q):
    """General web search (not limited to base44.app). Uses SearXNG / Firecrawl / Serper if configured."""
    import os
    urls = []
    sx = settings.get("searxng_url") or os.environ.get("SEARXNG_URL")
    try:
        if sx:
            j = json.loads(urllib.request.urlopen(sx.rstrip("/") + "/search?" + urllib.parse.urlencode(
                {"q": q, "format": "json", "language": "en-GB"}), timeout=30).read())
            urls = [r.get("url", "") for r in j.get("results", [])]
    except Exception: pass
    if not urls and settings.get("firecrawl_key"):
        try:
            body = json.dumps({"query": q, "limit": 10}).encode()
            req = urllib.request.Request("https://api.firecrawl.dev/v1/search", data=body, headers={
                "Authorization": f"Bearer {settings['firecrawl_key']}", "Content-Type": "application/json"})
            j = json.loads(urllib.request.urlopen(req, timeout=60).read())
            data = j.get("data", []); data = data.get("web", []) if isinstance(data, dict) else data
            urls = [r.get("url", "") for r in data]
        except Exception: pass
    if not urls and settings.get("serper_key"):
        try:
            req = urllib.request.Request("https://google.serper.dev/search", data=json.dumps({"q": q, "gl": "uk"}).encode(),
                                         headers={"X-API-KEY": settings["serper_key"], "Content-Type": "application/json"})
            urls = [o.get("link", "") for o in json.loads(urllib.request.urlopen(req, timeout=30).read()).get("organic", [])]
        except Exception: pass
    return [u for u in urls if u.startswith("http")]

def guess_domains(name):
    toks = name_tokens(name)
    full = re.sub(r"[^a-z0-9]", "", re.sub(r"\b(ltd|limited|llp|plc)\b", "", _collapse(name).replace(" and ", " ")))
    base = "".join(toks)
    cands = []
    for stem in dict.fromkeys([base, full.replace("limited", "").replace("ltd", ""), "-".join(toks), base + "ltd", toks[0] if toks else ""]):
        if stem and len(stem) >= 3:
            cands += [f"{stem}.co.uk", f"{stem}.com", f"{stem}.uk"]
    return list(dict.fromkeys(cands))[:12]

def find_website(company, settings):
    """Returns (url, how, confidence) or (None, reason, 0)."""
    name, city = company["name"], company.get("city", "")
    if not name_tokens(name): return None, "name too generic to match safely", 0
    tried = set()
    # a) search engine
    for q in (f'"{name}" {city}'.strip(), f"{name} {city} UK"):
        for u in search_urls(settings, q)[:10]:
            if is_directory(u): continue
            d = domain_of(u)
            if d in tried: continue
            tried.add(d)
            score = domain_match(name, u)
            if score >= 0.5:
                return "https://" + d if not u.startswith("http://") else "http://" + d, "search", round(min(1, score), 2)
    # b) smart domain guessing (free, no search engine needed)
    def check(dom):
        try:
            final, html = fetch("https://" + dom)
        except Exception:
            try: final, html = fetch("http://" + dom)
            except Exception: return None
        if is_directory(final) or "domain is for sale" in html.lower() or "parked" in html.lower()[:3000]: return None
        m = page_mentions(name, html)
        return (final, m) if m >= 0.5 else None
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        for dom, res in zip(guess_domains(name), ex.map(check, guess_domains(name))):
            if res:
                final, m = res
                p = urllib.parse.urlparse(final)
                return f"{p.scheme}://{p.netloc}", "guessed domain", round(min(1, m), 2)
    return None, "website not found", 0

# ---------------- 3. outdated-website audit ----------------
AUDIT_JS = """() => {
  const html = document.documentElement.outerHTML, txt = document.body ? document.body.innerText : '';
  const years = [...txt.matchAll(/(?:©|copyright|\\(c\\))\\s*(?:\\d{4}\\s*[-–]\\s*)?(\\d{4})/gi)].map(m=>+m[1]);
  const forms = [...document.querySelectorAll('form')];
  const hasContactForm = forms.some(f => f.querySelector('textarea, input[type=email]'));
  const quoteCta = [...document.querySelectorAll('a,button')].some(a => /quote|enquir|contact us|get in touch|request/i.test(a.innerText||''));
  const gen = (document.querySelector('meta[name=generator]')||{}).content || '';
  const jq = (window.jQuery && jQuery.fn && jQuery.fn.jquery) || '';
  const imgs = [...document.images]; const broken = imgs.filter(i => i.complete && i.naturalWidth === 0).length;
  return {
    viewport: !!document.querySelector('meta[name=viewport]'),
    copyright: years.length ? Math.max(...years) : null,
    contactForm: hasContactForm, quoteCta,
    metaDesc: !!document.querySelector('meta[name=description]'),
    ogImage: !!document.querySelector('meta[property="og:image"]'),
    generator: gen, jquery: jq,
    oldTags: /<(font|center|marquee|frameset|blink)[\\s>]/i.test(html),
    flash: /\\.swf|shockwave-flash/i.test(html),
    tableLayout: document.querySelectorAll('table table').length > 0 || document.querySelectorAll('td[width], table[width]').length > 5,
    brokenImages: broken, images: imgs.length, words: txt.split(/\\s+/).length,
    title: document.title
  };
}"""

def audit(page, url):
    """Load the homepage like a visitor would (desktop + phone) and list what makes it look outdated."""
    issues, info = [], {}
    t0 = time.time()
    try:
        resp = page.goto(url, wait_until="load", timeout=45000)
    except Exception as e:
        return {"load_error": str(e)[:150]}, [{"issue": "site_down", "detail": "website didn't load", "page": url}]
    info["load_s"] = round(time.time() - t0, 1)
    page.wait_for_timeout(1200)
    final = page.url
    info["https"] = final.startswith("https://")
    a = page.evaluate(AUDIT_JS)
    info.update(a)
    try:   # phone check
        page.set_viewport_size({"width": 390, "height": 844}); page.wait_for_timeout(800)
        info["mobile_overflow"] = page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 8")
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})
    yr = datetime.date.today().year
    if not info["https"]: issues.append({"issue": "no_https", "detail": "no padlock: browsers mark the site 'Not secure'"})
    if not a["viewport"] or info.get("mobile_overflow"):
        issues.append({"issue": "not_mobile_friendly", "detail": "the site doesn't fit on a phone screen" + ("" if a["viewport"] else " (no mobile setup at all)")})
    if info["load_s"] > 6: issues.append({"issue": "slow_site", "detail": f"homepage took {info['load_s']}s to load"})
    if a["copyright"] and a["copyright"] <= yr - 3: issues.append({"issue": "old_copyright", "detail": f"footer still says © {a['copyright']}"})
    if not a["contactForm"] and not a["quoteCta"]: issues.append({"issue": "no_quote_option", "detail": "no quote button or enquiry form on the homepage"})
    elif not a["contactForm"]: issues.append({"issue": "no_contact_form", "detail": "no enquiry form on the homepage"})
    if a["oldTags"] or a["flash"] or a["tableLayout"]: issues.append({"issue": "dated_build", "detail": "built with very old web techniques (tables/old tags)"})
    if a["jquery"].startswith("1."): issues.append({"issue": "dated_build", "detail": f"uses jQuery {a['jquery']} (from around 2010-2016)"})
    if a["brokenImages"]: issues.append({"issue": "broken_images", "detail": f"{a['brokenImages']} broken image(s) on the homepage"})
    if not a["metaDesc"]: issues.append({"issue": "weak_google_listing", "detail": "no description for Google search results"})
    for i in issues: i["page"] = final
    return info, issues

ISSUE_WEIGHT = {"site_down": 5, "not_mobile_friendly": 4, "no_https": 3, "no_quote_option": 3, "dated_build": 3,
                "old_copyright": 2, "slow_site": 2, "broken_images": 2, "no_contact_form": 1, "weak_google_listing": 1}
HOOK = {
    "not_mobile_friendly": "your website doesn't fit on a phone screen, and most buyers now find suppliers on their phone first.",
    "no_https": "your website shows as 'Not secure' in Chrome and Edge, which can make buyers hesitate before sending an enquiry.",
    "no_quote_option": "there's no quick way to request a quote from your homepage, so buyers who are ready to order have to dig for contact details.",
    "dated_build": "the website looks quite dated next to your competitors, which doesn't reflect the quality of your work.",
    "old_copyright": "the footer still shows an old copyright year, which can make visitors wonder if the business is still active.",
    "slow_site": "your homepage is slow to load, and buyers often leave before it finishes.",
    "broken_images": "some images on your homepage are broken.",
    "site_down": "your website didn't load when I tried it.",
    "no_contact_form": "there's no enquiry form on your homepage.",
    "weak_google_listing": "your site has no description for Google results, so it's easy to overlook in search.",
}

def hook_and_bullets(issues):
    ranked = sorted({i["issue"] for i in issues if i["issue"] in HOOK}, key=lambda k: -ISSUE_WEIGHT.get(k, 1))
    hook = HOOK[ranked[0]] if ranked else "the website could be doing a lot more to win you enquiries."
    bullets = "\n".join("- " + HOOK[k][0].upper() + HOOK[k][1:].split(", so ")[0].split(", which")[0].split(", and ")[0].rstrip(".")
                        for k in ranked[:4]) or "- A few quick wins on design, mobile and enquiries"
    return hook, bullets

DEFAULT_COMPANY_SEQUENCES = {
    "email": [
        {"day": 0, "subject": "Quick idea for the {business} website",
         "body": "Hi,\n\nI came across {business} on Europages and had a look at your website ({site_short}). I noticed {hook}\n\n"
                 "I design modern websites for UK manufacturers and suppliers that make it easy for buyers to request a quote, "
                 "including from their phone.\n\nWould it be useful if I put together a free mockup of a refreshed {business} homepage, "
                 "so you can see the difference?\n\n{signature}"},
        {"day": 4, "subject": "",
         "body": "Hi again,\n\nHere's what stood out when I looked at {site_short}:\n{issues_bullets}\n\n"
                 "All of that is fixable, and a refresh usually starts from £{price}. Happy to send the free mockup first, no obligation.\n\n{signature}"},
        {"day": 9, "subject": "",
         "body": "Hi,\n\nOne more thought: a clear \"Request a quote\" button and a site that works properly on phones are usually the "
                 "two changes that bring in the most new enquiries for suppliers like {business}.\n\nShall I put the mockup together?\n\n{signature}"},
        {"day": 15, "subject": "",
         "body": "Hi,\n\nI'll leave it here so I don't fill your inbox. If you'd like the free homepage mockup at any point, "
                 "just reply to this email.\n\nAll the best,\n{name}"},
    ],
    "whatsapp": [
        {"day": 0, "body": "Hi, is this {business}? I had a look at your website and noticed {hook} I put together modern sites for UK suppliers. Would a free mockup of a refreshed homepage be useful?"},
        {"day": 3, "body": "Hi again, just checking you saw my message about the {business} website? Happy to send the free mockup."},
    ],
}

def short_name(name):
    """'Unifabs Limited' -> 'Unifabs' for friendlier messages."""
    s = re.sub(r"[\s,]+(ltd\.?|limited|llp|plc|\(uk\)|uk ltd)\s*$", "", name.strip(), flags=re.I).strip()
    return s or name

def normalise_site(url):
    p = urllib.parse.urlparse(url if "//" in url else "https://" + url)
    return f"{p.scheme or 'https'}://{p.netloc}"

def company_key(url, name):
    d = domain_of(url) if url else re.sub(r"[^a-z0-9]", "", name.lower())[:30]
    return "co-" + re.sub(r"[^a-z0-9]+", "-", d).strip("-")


# ================= Automatic company finding with Firecrawl (no manual directory browsing) =================
FC = "https://api.firecrawl.dev"
EXCLUDE_DOMAINS = ["yell.com", "europages.co.uk", "europages.com", "checkatrade.com", "trustatrader.com", "mybuilder.com",
                   "ratedpeople.com", "bark.com", "facebook.com", "linkedin.com", "instagram.com", "x.com", "twitter.com",
                   "youtube.com", "tiktok.com", "companieshouse.gov.uk", "gov.uk", "endole.co.uk", "thomsonlocal.com",
                   "192.com", "freeindex.co.uk", "yelp.co.uk", "yelp.com", "trustpilot.com", "indeed.com", "indeed.co.uk",
                   "glassdoor.co.uk", "wikipedia.org", "amazon.co.uk", "ebay.co.uk", "kompass.com", "applegate.co.uk",
                   "scoot.co.uk", "cylex-uk.co.uk", "hotfrog.co.uk", "bizdb.co.uk", "opencorporates.com", "find-and-update.company-information.service.gov.uk"]

def _fc_post(key, path, body, timeout=90):
    req = urllib.request.Request(FC + path, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def _results(j):
    data = j.get("data", [])
    if isinstance(data, dict): data = data.get("web", [])
    return data or []

def fc_search_companies(key, sector, town, limit=10):
    """UK company websites for '<sector> <town>' (directories excluded). 2 Firecrawl credits per 10 results."""
    q = f"{sector} {town}".strip() + " UK company"
    body = {"query": q, "limit": limit, "country": "GB", "location": "United Kingdom", "excludeDomains": EXCLUDE_DOMAINS[:40]}
    try:
        res = _results(_fc_post(key, "/v2/search", body))
    except urllib.error.HTTPError as e:
        if e.code not in (400, 404, 422): raise
        res = _results(_fc_post(key, "/v1/search", {"query": q, "limit": limit, "country": "gb", "location": "United Kingdom"}))
    out, seen = [], set()
    for r in res:
        url = r.get("url") or (r.get("metadata") or {}).get("sourceURL") or ""
        if not url.startswith("http") or is_directory(url): continue
        dom = domain_of(url)
        if dom in seen or any(dom == x or dom.endswith("." + x) for x in EXCLUDE_DOMAINS): continue
        seen.add(dom)
        name = name_from_title(r.get("title") or "", dom)
        out.append({"name": name, "city": town.title(), "country": "United Kingdom", "website": normalise_site(url),
                    "type": "", "size": "", "about": (r.get("description") or "")[:300], "found": "firecrawl search", "sector": sector})
    return out

GENERIC_TITLE = re.compile(r"^(home|homepage|welcome|index|about us|contact( us)?|services|untitled)$", re.I)
def name_from_title(title, dom):
    parts = [re.sub(r"^(welcome to|home of|home)\s+", "", p.strip(), flags=re.I) for p in re.split(r"\s+[|–—\-:•·]\s+", title or "") if p.strip()]
    parts = [p for p in parts if not GENERIC_TITLE.match(p) and len(p) <= 60]
    toks = [t for t in re.split(r"[^a-z0-9]+", dom.split(".")[0]) if t]
    for p in parts:                                  # prefer the part that matches the web address
        low = re.sub(r"[^a-z0-9]", "", p.lower())
        if any(t in low for t in toks if len(t) > 2) or low[:6] in dom.replace("-", ""): return p
    if parts: return parts[0]
    return dom.split(".")[0].replace("-", " ").title()

def _contact_links_from_html(html):
    return [{"href": h, "text": ""} for h in re.findall(r'href=["\'](tel:[^"\']+|mailto:[^"\']+|https?://(?:wa\.me|api\.whatsapp\.com)/[^"\']+)', html or "", re.I)]

def fc_scrape(key, url):
    """One page via Firecrawl (1 credit). Returns a page dict in the same shape Scout's own scanner produces, plus raw HTML."""
    body = {"url": url, "formats": ["markdown", "links", "rawHtml"], "onlyMainContent": False, "timeout": 45000,
            "location": {"country": "GB", "languages": ["en-GB"]}}
    try:
        j = _fc_post(key, "/v2/scrape", body)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 404, 422): raise
        j = _fc_post(key, "/v1/scrape", {"url": url, "formats": ["markdown", "links", "rawHtml"], "onlyMainContent": False})
    d = j.get("data", {}) or {}
    meta = d.get("metadata", {}) or {}
    title = meta.get("title") or ""
    if isinstance(title, list): title = title[0] if title else ""
    links = [{"href": l if isinstance(l, str) else l.get("url", ""), "text": ""} for l in (d.get("links") or [])]
    raw = d.get("rawHtml") or d.get("html") or ""
    return {"url": meta.get("url") or meta.get("sourceURL") or url, "title": title, "text": d.get("markdown") or "",
            "links": links + _contact_links_from_html(raw), "raw": raw, "status": meta.get("statusCode")}

def fc_scan_site(key, url, pages=2):
    """Homepage + best contact/about page through Firecrawl (~2 credits)."""
    home = fc_scrape(key, url)
    out = [home]
    if pages > 1:
        host = domain_of(home["url"])
        cand = [l["href"] for l in home["links"] if l["href"].startswith("http") and domain_of(l["href"]) == host
                and not re.search(r"/wp-(content|includes|json|admin)|/assets?/|/static/|/cdn-cgi/|\.(js|css|png|jpe?g|gif|svg|webp|pdf|ico|xml)(\?|$)", l["href"], re.I)
                and re.search(r"contact|get-in-touch|about|quote|enquir", urllib.parse.urlparse(l["href"]).path, re.I)]
        cand.sort(key=lambda u: (0 if re.search(r"contact|get-in-touch|enquir", u, re.I) else 1, len(u)))
        if cand:
            try: out.append(fc_scrape(key, cand[0]))
            except Exception as e: out.append({"url": cand[0], "error": str(e)[:120]})
    return out

def audit_html(html, final_url):
    """Outdated-site checks from the page HTML (used when Firecrawl does the visiting instead of Scout's browser)."""
    issues, info = [], {}
    low = html.lower()
    yr = datetime.date.today().year
    info["https"] = final_url.startswith("https://")
    info["viewport"] = bool(re.search(r'<meta[^>]+name=["\']viewport', html, re.I))
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright|\(c\))\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", html, re.I)]
    info["copyright"] = max(years) if years else None
    info["contactForm"] = bool(re.search(r"<form[\s\S]{0,4000}?(<textarea|type=[\"']email)", html, re.I))
    info["quoteCta"] = bool(re.search(r">\s*[^<]{0,40}(quote|enquir|contact us|get in touch|request)[^<]{0,40}<", html, re.I))
    jq = re.search(r"jquery[.-]?(\d\.\d+(?:\.\d+)?)", low)
    info["jquery"] = jq.group(1) if jq else ""
    info["oldTags"] = bool(re.search(r"<(font|center|marquee|frameset|blink)[\s>]", low))
    info["tableLayout"] = len(re.findall(r"<td[^>]+width=", low)) > 5 or "<table" in low and low.count("<table") > 3
    info["metaDesc"] = bool(re.search(r'<meta[^>]+name=["\']description', html, re.I))
    if not info["https"]: issues.append({"issue": "no_https", "detail": "no padlock: browsers mark the site 'Not secure'"})
    if not info["viewport"]: issues.append({"issue": "not_mobile_friendly", "detail": "the site isn't set up for phones (no mobile layout)"})
    if info["copyright"] and info["copyright"] <= yr - 3: issues.append({"issue": "old_copyright", "detail": f"footer still says © {info['copyright']}"})
    if not info["contactForm"] and not info["quoteCta"]: issues.append({"issue": "no_quote_option", "detail": "no quote button or enquiry form on the homepage"})
    elif not info["contactForm"]: issues.append({"issue": "no_contact_form", "detail": "no enquiry form on the homepage"})
    if info["oldTags"] or info["tableLayout"]: issues.append({"issue": "dated_build", "detail": "built with very old web techniques (tables/old tags)"})
    if info["jquery"].startswith("1."): issues.append({"issue": "dated_build", "detail": f"uses jQuery {info['jquery']} (from around 2010-2016)"})
    if not info["metaDesc"]: issues.append({"issue": "weak_google_listing", "detail": "no description for Google search results"})
    seen = set(); issues = [i for i in issues if not (i["issue"] in seen or seen.add(i["issue"]))]
    for i in issues: i["page"] = final_url
    info["checked_by"] = "firecrawl"
    return info, issues

DEFAULT_CO_AUTO = {"enabled": False, "daily_target": 20, "sectors": [s[1] for s in SECTORS[:6]],
                   "towns": ["London", "Birmingham", "Manchester", "Leeds", "Sheffield", "Bristol", "Nottingham", "Leicester",
                             "Coventry", "Bradford", "Liverpool", "Newcastle", "Glasgow", "Cardiff", "Stoke-on-Trent", "Wolverhampton"],
                   "per_search": 10, "auto_outreach": False, "min_score": 3, "min_quality": 60, "scan_with": "builtin", "max_searches_per_day": 20}
