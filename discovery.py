"""
Discovery: find sites on website-builder platforms (Base44, Lovable, Replit, Emergent, Bolt, Netlify, Vercel…) WITHOUT paying for a search API.

Keyword search (targeted, best quality), tried in order, using whatever you have:
  1. SearXNG  - free search engine you run yourself (included in docker-compose). Queries Google/Bing/DDG/Brave.
  2. Firecrawl - optional, needs a key (paid after free credits)
  3. Serper    - optional, needs a key

Bulk sources (free, no key, thousands of sites; refreshed about once a day):
  * Common Crawl index   * Internet Archive (Wayback) index   * Certificate Transparency logs (crt.sh)
Bulk sites go into a "candidate pool". Each one gets a cheap pre-check (page title only, ~0.3s) so tools and
hobby apps are dropped before the slow full scan.
"""
import json, re, time, random, urllib.request, urllib.parse, concurrent.futures, datetime
import outreach, platforms

UA = "Mozilla/5.0 (compatible; LeadScout/1.0)"
ONLY = {"domains": None}
META = {}                    # host -> title + snippet seen in search results (used to judge UK-ness before scanning)

FOREIGN_RE = re.compile(r"\b(nigeria|lagos|abuja|ghana|accra|kenya|nairobi|south africa|johannesburg|india|mumbai|delhi|pakistan|"
                        r"usa|united states|florida|texas|california|new york|canada|toronto|australia|sydney|melbourne|dubai|uae|"
                        r"ireland|dublin|philippines|malaysia|singapore)\b|\+234|\+1[\s(-]?\d{3}|\+91|\+61|\$\s?\d", re.I)
UK_HINT_RE = re.compile(r"\b(uk|united kingdom|england|scotland|wales|london|manchester|birmingham|leeds|liverpool|bristol|glasgow|"
                        r"edinburgh|sheffield|nottingham|leicester|cardiff|newcastle|kent|essex|surrey|yorkshire)\b|£|\+44|\b07\d{3}", re.I)

def uk_hint(text):
    """+ for UK-looking, - for clearly foreign, 0 for unknown."""
    t = text or ""
    return (2 if UK_HINT_RE.search(t) else 0) - (2 if FOREIGN_RE.search(t) else 0)

def _remember(url, *parts):
    h = norm_host(url)
    if h: META[h] = " ".join(p for p in parts if p)[:500]
    return url     # set per call so bulk sources only accept their own platform

def _get(url, timeout=40, headers=None, data=None):
    h = {"User-Agent": UA, **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def norm_host(u):
    return platforms.site_host(u, ONLY["domains"])

def hosts_from(urls):
    out = []
    for u in urls:
        h = norm_host(u)
        if h and h not in out: out.append(h)
    return out

# ---------------- keyword search providers ----------------
def searxng(base, q):
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": q, "format": "json", "language": "en-GB", "safesearch": 0})
    res = json.loads(_get(url, timeout=40)).get("results", [])
    return hosts_from(_remember(r.get("url", ""), r.get("title"), r.get("content")) for r in res)

def firecrawl(key, q):
    body = json.dumps({"query": q, "limit": 20}).encode()
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for ep in ("https://api.firecrawl.dev/v2/search", "https://api.firecrawl.dev/v1/search"):
        try:
            j = json.loads(_get(ep, timeout=60, headers=hdr, data=body))
        except urllib.error.HTTPError as e:
            if e.code == 404: continue
            raise
        data = j.get("data", [])
        if isinstance(data, dict): data = data.get("web", [])      # v2 shape
        return hosts_from(_remember(r.get("url", ""), r.get("title"), r.get("description")) for r in data)
    return []

def serper(key, q):
    body = json.dumps({"q": q, "gl": "uk", "num": 10}).encode()
    j = json.loads(_get("https://google.serper.dev/search", headers={"X-API-KEY": key, "Content-Type": "application/json"}, data=body))
    return hosts_from(_remember(o.get("link", ""), o.get("title"), o.get("snippet")) for o in j.get("organic", []))

def keyword_providers(settings):
    import os
    p = []
    sx = settings.get("searxng_url") or os.environ.get("SEARXNG_URL")
    if sx: p.append(("searxng", lambda q: searxng(sx, q)))
    if settings.get("firecrawl_key"): p.append(("firecrawl", lambda q: firecrawl(settings["firecrawl_key"], q)))
    if settings.get("serper_key"): p.append(("serper", lambda q: serper(settings["serper_key"], q)))
    return p

def search(settings, q, log=print):
    """Try each available keyword provider until one returns results. Returns (provider, hosts)."""
    errors = []
    for name, fn in keyword_providers(settings):
        try:
            hosts = fn(q)
            if hosts: return name, hosts
            errors.append(f"{name}: 0 results")
        except Exception as e:
            errors.append(f"{name}: {str(e)[:80]}")
    if errors: log("search: " + " | ".join(errors))
    return None, []

# ---------------- bulk sources (free archives), per platform ----------------
MAX_PER_TASK = 3000          # keep the pool (and the data file) a sensible size; big platforms are sampled

def common_crawl(domain, max_pages=2):
    """Hosts from the newest Common Crawl indexes (the index server is often busy, so retry)."""
    cols = json.loads(_retry(lambda: _get("https://index.commoncrawl.org/collinfo.json", 60)))
    hosts = []
    for col in cols[:1]:
        for page in range(max_pages):
            url = f"https://index.commoncrawl.org/{col['id']}-index?" + urllib.parse.urlencode(
                {"url": f"*.{domain}", "output": "json", "fl": "url", "page": page})
            try:
                txt = _retry(lambda: _get(url, 120))
            except urllib.error.HTTPError as e:
                if e.code in (400, 404): break        # no more pages
                raise
            urls = []
            for line in txt.splitlines():
                try: urls.append(json.loads(line)["url"])
                except Exception: pass
            if not urls: break
            hosts += [h for h in hosts_from(urls) if h not in hosts]
    return hosts

def wayback(domain, limit=40000):
    url = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(
        {"url": f"*.{domain}", "fl": "original", "collapse": "urlkey", "limit": limit})
    return hosts_from(_retry(lambda: _get(url, 120)).splitlines())

def crtsh(domain):
    j = json.loads(_retry(lambda: _get(f"https://crt.sh/?q=%25.{domain}&output=json", 120)))
    names = set()
    for row in j:
        names.update(row.get("name_value", "").split())
    return hosts_from(names)

def _retry(fn, tries=2):
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in (400, 404) or i == tries - 1: raise
        except Exception:
            if i == tries - 1: raise
        time.sleep(5 * (i + 1) + random.random() * 3)

BULK = {"commoncrawl": common_crawl, "wayback": wayback, "crtsh": crtsh}

def bulk_due(d):
    """Tasks like 'wayback:lovable.app' that haven't run today (each retried at most every 3 hours)."""
    today = outreach.uk_now().date().isoformat(); slot = today + "@" + str(outreach.uk_now().hour // 3)
    st = d.get("discovery", {})
    return [f"{src}:{dom}" for dom in platforms.enabled(d.get("settings")) for src in BULK
            if st.get(f"{src}:{dom}_last") != today and st.get(f"{src}:{dom}_tried") != slot]

def fetch_bulk(names, log=print):
    """Network only (no shared data touched). Returns {task: hosts list | Exception}."""
    out = {}
    for n in names:
        src, dom = n.split(":", 1)
        try:
            ONLY["domains"] = [dom]
            hosts = BULK[src](dom)
            if len(hosts) > MAX_PER_TASK: hosts = random.sample(hosts, MAX_PER_TASK)
            out[n] = hosts; log(f"discovery: {src} found {len(hosts)} {platforms.LABEL.get(dom, dom)} sites")
        except Exception as e:
            out[n] = e; log(f"discovery: {src} unavailable for {dom} ({str(e)[:60]}), will retry later")
        finally:
            ONLY["domains"] = None
    return out

def merge_bulk(d, results, known=()):
    pool = d.setdefault("candidates", {}); st = d.setdefault("discovery", {})
    today = outreach.uk_now().date().isoformat(); slot = today + "@" + str(outreach.uk_now().hour // 3); added = 0
    for n, hosts in results.items():
        src = n.split(":", 1)[0]
        st[f"{n}_tried"] = slot
        if isinstance(hosts, Exception):
            st[f"{n}_error"] = st[f"{src}_error"] = f"{today}: {str(hosts)[:120]}"; continue
        st[f"{n}_last"] = today; st[f"{n}_error"] = None; st[f"{n}_count"] = len(hosts)
        st[f"{src}_last"] = today; st[f"{src}_error"] = None
        st[f"{src}_count"] = sum(v for k, v in st.items() if k.startswith(src + ":") and k.endswith("_count") and isinstance(v, int))
        for h in hosts:
            if h not in pool and platforms.lead_key(h) not in known:
                pool[h] = {"src": src, "platform": platforms.platform_of(h), "added": today, "status": "new"}; added += 1
    return added

def refresh_pool(d, log=print, known=()):
    return merge_bulk(d, fetch_bulk(bulk_due(d), log), known)

# ---------------- cheap pre-check ----------------
APP_WORDS = re.compile(r"\b(dashboard|tracker|planner|generator|calculator|quiz|game|tool|tools|ai\b|gpt|crm|saas|admin|"
                       r"portal|login|app|todo|habit|budget|notes|chat|bot|demo|test|template|prototype|mvp|hackathon|"
                       r"inventory|analytics|erp|lms|course|student|journal)\b", re.I)
BIZ_WORDS = re.compile(r"\b(clean|cleaning|cleaners|plumb|electric|handyman|builder|building|roof|garden|landscap|removal|"
                       r"barber|salon|hair|nails?|lash|brow|beauty|spa|massage|aesthetic|tattoo|dog|groom|pet|vet|"
                       r"photograph|wedding|florist|bakery|cafe|restaurant|takeaway|catering|tutor|tuition|driving|"
                       r"mechanic|garage|car|valet|detailing|decorat|painter|carpent|joiner|plaster|tiler|kitchen|"
                       r"bathroom|lock|security|cctv|fitness|personal trainer|gym|yoga|pilates|physio|clinic|dental|"
                       r"accountant|solicitor|estate|letting|property|maintenance|construction|services|ltd|limited)\b", re.I)
UK_WORDS = re.compile(r"\b(uk|london|manchester|birmingham|leeds|liverpool|bristol|sheffield|glasgow|edinburgh|nottingham|"
                      r"leicester|cardiff|newcastle|southampton|brighton|essex|kent|surrey|sussex|yorkshire|lancashire|"
                      r"midlands|wales|scotland|england|bolton|reading|oxford|cambridge|coventry|bradford|belfast|"
                      r"milton keynes|luton|swindon|portsmouth|plymouth|derby|stoke|wolverhampton|norwich|exeter|bath|york)\b", re.I)

def precheck(host):
    """Fetch only the raw HTML title. Returns (keep: bool, title, reason)."""
    try:
        html = _get(f"https://{host}/", timeout=15)
    except Exception as e:
        return False, "", f"unreachable: {str(e)[:40]}"
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    md = re.search(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]*content=["\']([^"\']*)', html, re.I)
    desc = md.group(1) if md else ""
    if uk_hint(title + " " + desc) < 0:
        return False, title, "looks like it's outside the UK"
    if not title or title.lower() in ("base44", "base44 app", "untitled", "lovable", "lovable app", "vite + react", "vite + react + ts",
                                      "react app", "replit", "my app", "home", "document", "index", "create next app", "next.js", "bolt", "emergent"):
        return False, title, "no title"
    biz, uk, app = bool(BIZ_WORDS.search(title + " " + desc)), bool(UK_WORDS.search(title + " " + desc)), bool(APP_WORDS.search(title))
    if uk or (biz and not app):
        return True, title, "business" + (" + UK" if uk else "")
    if app:
        return False, title, "looks like an app/tool"
    return "maybe", title, "unclear name"           # e.g. "Handy Titans": scanned after the clear ones

def next_from_pool(d, n, log=print, workers=8):
    """Pre-check pool candidates until n good ones are found. Returns list of https URLs to fully scan."""
    pool = d.get("candidates", {})
    keep = [h for h, c in pool.items() if c["status"] == "keep"][:n]      # pre-checked earlier, not scanned yet
    todo = [h for h, c in pool.items() if c["status"] == "new"]
    random.shuffle(todo)
    for i in range(0, len(todo), workers * 4):
        batch = todo[i:i + workers * 4]
        with concurrent.futures.ThreadPoolExecutor(workers) as ex:
            for h, (ok, title, why) in zip(batch, ex.map(precheck, batch)):
                pool[h].update(status="keep" if ok is True else "maybe" if ok == "maybe" else "skip", title=title[:120], why=why)
                if ok is True: keep.append(h)
        if len(keep) >= n: break
    if len(keep) < n:                                   # not enough clear businesses: use the unclear ones too
        keep += [h for h, c in pool.items() if c["status"] == "maybe" and h not in keep][:n - len(keep)]
    if keep: log(f"discovery: pre-checked pool, {len(keep)} look like businesses")
    for h in keep[:n]: pool[h]["status"] = "scanned"
    return ["https://" + h for h in keep[:n]]

def pool_stats(d):
    pool = d.get("candidates", {})
    c = {"total": len(pool), "new": 0, "keep": 0, "maybe": 0, "skip": 0, "scanned": 0}
    for v in pool.values(): c[v["status"]] = c.get(v["status"], 0) + 1
    return c
