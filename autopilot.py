"""
Autopilot: hit a daily target of NEW leads put into outreach.

Each engine pass (every few minutes, while app.py is running):
  1. Count how many leads were started today.
  2. If short of the target, enroll the best eligible leads already scanned (highest "unfinished" score first).
  3. If the pool is empty and auto-discover is on: run the next Google searches (Serper), scan the new
     base44.app sites, then enroll from those. Search and scan budgets per day stop it running away.
Emails then go out through the normal engine (window + daily cap); WhatsApp steps appear in Today.
"""
import json, re, urllib.request, datetime
import outreach, discovery, platforms, quality, stopper

DEFAULT_AUTOPILOT = {
    "enabled": False,
    "daily_target": 20,                 # new leads started per day
    "channels": ["whatsapp", "email"],  # which sequences to start
    "auto_discover": True,              # find + scan new sites when the pool runs out (free sources, no key needed)
    "max_searches_per_day": 15,
    "max_scans_per_day": 80,
    "min_score": 1,                     # skip sites that look finished
    "min_quality": 60,                  # Lead Quality (0-100): only start leads likely to convert
    "niches": ["cleaning", "cleaner", "handyman", "plumber", "electrician", "barber", "salon", "nails", "lashes",
               "beauty", "builder", "roofer", "gardener", "landscaping", "removals", "dog grooming",
               "personal trainer", "tutor", "photographer", "driving instructor", "mobile mechanic", "decorator"],
    "cities": ["London", "Manchester", "Birmingham", "Leeds", "Liverpool", "Bristol", "Sheffield", "Glasgow",
               "Edinburgh", "Nottingham", "Leicester", "Cardiff", "Newcastle", "Southampton", "Brighton", "UK"],
}

BROKEN_SITE_PATTERNS = ['"lorem ipsum" UK', '"coming soon" "07"', '"your business name"', '"example.com" UK', '"07000"', 'whatsapp "+44"']
def broken_site_queries(domains):
    return [f"site:{dom} {p}" for p in BROKEN_SITE_PATTERNS for dom in domains]

def today(): return outreach.uk_now().date().isoformat()

def settings(d):
    return {**DEFAULT_AUTOPILOT, **d["settings"].get("autopilot", {})}

def day_stats(d):
    st = d.setdefault("autopilot_days", {})
    t = st.setdefault(today(), {"enrolled": 0, "searches": 0, "scans": 0, "keys": [], "notes": []})
    for old in sorted(st)[:-30]: st.pop(old)        # keep 30 days of history
    return t

def contacts_of(l):
    return {c for c in (l.get("number"), (l.get("email") or "").lower()) if c}

def already_contacted(d):
    """Phone numbers / emails of every lead already in outreach, so the same business is never contacted twice
    (e.g. 'ProRail' and 'ProRail (Copy)' on two different sites)."""
    used = set()
    for l in d["leads"].values():
        if l.get("history") or l.get("seq", {}).get("whatsapp") or l.get("seq", {}).get("email") or l.get("status") not in ("new", None):
            used |= contacts_of(l)
    return used

def eligible(d, ap):
    out, used = [], already_contacted(d)
    cands = []; q = quality.score_all(d)
    for k, l in d["leads"].items():
        if l.get("status") != "new" or l.get("seq", {}).get("whatsapp") or l.get("seq", {}).get("email"): continue
        if l.get("source") == "company": continue          # companies are started from the Companies tab
        if l["scan"].get("competitor") or l["scan"].get("offline"): continue   # web designers/agencies, dead sites
        if l["scan"].get("unfinished_score", 0) < ap["min_score"]: continue
        chans = [c for c in ap["channels"] if (c == "email" and outreach.can_email(l, d)) or
                 (c == "whatsapp" and outreach.can_whatsapp(l, d) and l.get("opener"))]
        if q[k]["score"] < ap.get("min_quality", 60): continue           # not likely enough to convert
        if chans: cands.append((q[k]["score"] * 100 + l["scan"]["unfinished_score"], k, chans))   # best leads first
    cands.sort(reverse=True)
    for score, k, chans in cands:                        # best copy of each business only
        cs = contacts_of(d["leads"][k])
        if cs & used: continue
        used |= cs; out.append((score, k, chans))
    return out

def enroll_some(d, ap, n):
    stats = day_stats(d); started = 0
    es = d["settings"]["email"]
    for _, k, chans in eligible(d, ap)[:n]:
        l = d["leads"][k]
        if "whatsapp" in chans: outreach.start(l, "whatsapp", d)
        if "email" in chans:
            outreach.start(l, "email", d, es.get("email_delay_days_if_whatsapp", 1) if "whatsapp" in chans else 0)
        stats["enrolled"] += 1; stats["keys"].append(k); started += 1
    return started

def next_queries(d, ap, n):
    """Rotate through broken-site queries + niche x city combos, skipping ones used in the last 30 days."""
    used = d.setdefault("queries_used", {})
    cutoff = (outreach.uk_now() - datetime.timedelta(days=30)).date().isoformat()
    doms = platforms.enabled(d.get("settings")) or ["base44.app"]
    combos = [f"site:{dom} {n_} {c}" for c in ap["cities"] for n_ in ap["niches"] for dom in doms]   # UK-targeted, every platform
    broken = broken_site_queries(doms)
    pool = []
    for i, q in enumerate(combos):
        pool.append(q)
        if i % 8 == 7 and broken: pool.append(broken[(i // 8) % len(broken)])
    fresh = [q for q in pool if used.get(q, "") < cutoff]
    return fresh[:n]

def run(load, save, lock, log, scan_urls, key_of):
    """One autopilot pass. scan_urls(list) scans sites and stores them as leads (blocking)."""
    with lock:
        d = load(); ap = settings(d)
        if not ap["enabled"] or stopper.stale(): return
        es = d["settings"]["email"]
        now = outreach.uk_now()
        if es.get("weekdays_only", True) and now.weekday() >= 5: return
        if not (7 <= now.hour < int(es.get("end_hour", 17))): return
        stats = day_stats(d)
        need = ap["daily_target"] - stats["enrolled"]
        if need <= 0: save(d); return
        got = enroll_some(d, ap, need); need -= got
        save(d)
    if got: log(f"autopilot: started {got} leads (today {stats['enrolled']}/{ap['daily_target']})")
    if need <= 0 or not ap["auto_discover"]: return
    providers = discovery.keyword_providers(d["settings"])
    for _ in range(3):
        with lock:
            d = load(); s = day_stats(d)
            need = ap["daily_target"] - s["enrolled"]
            q_left = ap["max_searches_per_day"] - s["searches"]; scan_left = ap["max_scans_per_day"] - s["scans"]
            if need <= 0: return
            if scan_left <= 0:
                if "budget used" not in s["notes"]: s["notes"].append("budget used")
                save(d); return
            queries = next_queries(d, ap, min(3, q_left)) if providers and q_left > 0 else []
            known = set(d["leads"]) | set(d.get("deleted", []))     # never bring back leads you deleted
        want = min(scan_left, max(need * 3, 10))
        urls = []
        # 1. keyword search (SearXNG / Firecrawl / Serper)
        for q in queries:
            if stopper.stale(): return
            prov, hosts = discovery.search(d["settings"], q, log)
            with lock:
                d = load(); d.setdefault("queries_used", {})[q] = today(); day_stats(d)["searches"] += 1; save(d)
            urls += ["https://" + h for h in hosts if key_of("https://" + h) not in known and "https://" + h not in urls]
            if prov: log(f"autopilot: '{q}' via {prov} -> {len(hosts)} sites")
        if stopper.stale(): return
        # 2. free bulk pool (Common Crawl, Internet Archive, certificate logs)
        if len(urls) < want:
            with lock:
                d = load(); ps = discovery.pool_stats(d)
                due = discovery.bulk_due(d) if ps["new"] + ps["keep"] < want * 5 else []
            if due:
                res = discovery.fetch_bulk(due, log)                     # slow network work, outside the lock
                with lock:
                    d = load(); discovery.merge_bulk(d, res, known); save(d)
            snap = load()                                                # pre-check on a snapshot, outside the lock
            picked = discovery.next_from_pool(snap, want - len(urls), log)
            with lock:
                d = load(); pool = d.setdefault("candidates", {})
                for h, c in snap.get("candidates", {}).items():
                    if c["status"] != "new" and h in pool: pool[h].update(c)
                save(d)
            urls += [u for u in picked if key_of(u) not in known and u not in urls]
        # judge by the search snippet first: UK-looking results go to the front, clearly foreign ones are dropped
        urls = [u for u in urls if discovery.uk_hint(discovery.META.get(platforms.host_of(u), "")) >= 0]
        urls.sort(key=lambda u: -discovery.uk_hint(discovery.META.get(platforms.host_of(u), "")))
        urls = urls[:want]
        if not urls:
            log("autopilot: no new sites found right now; will try again later"); return
        if stopper.stale(): return
        log(f"autopilot: scanning {len(urls)} new sites")
        scan_urls(urls)
        if stopper.stale(): return
        with lock:
            d = load(); day_stats(d)["scans"] += len(urls)
            stats = day_stats(d); need = ap["daily_target"] - stats["enrolled"]
            got = enroll_some(d, ap, need) if need > 0 else 0; save(d)
        if got: log(f"autopilot: started {got} more leads (today {stats['enrolled']}/{ap['daily_target']})")
        if stats["enrolled"] >= ap["daily_target"]: return
