"""
Website builders / hosts Scout looks for. Sites still on these free addresses are often unfinished DIY projects.
Add a platform here (or switch one on in Settings → Platforms) and discovery, Autopilot, scanning and messages all use it.
"""
import re
from urllib.parse import urlparse

PLATFORMS = [   # domain, label, on by default
    ("base44.app", "Base44", True),
    ("lovable.app", "Lovable", True),
    ("replit.app", "Replit", True),
    ("emergent.host", "Emergent", True),
    ("bolt.host", "Bolt", True),
    ("netlify.app", "Netlify", True),
    ("vercel.app", "Vercel", True),
    ("github.io", "GitHub Pages", False),
    ("framer.website", "Framer", False),
    ("webflow.io", "Webflow", False),
    ("web.app", "Firebase", False),
    ("onrender.com", "Render", False),
    ("carrd.co", "Carrd", False),
]
DOMAINS = [p[0] for p in PLATFORMS]
LABEL = {p[0]: p[1] for p in PLATFORMS}
RESERVED = {"www", "app", "api", "docs", "help", "blog", "status", "admin", "dashboard", "preview", "support", "mail"}
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

def enabled(settings):
    """Domains switched on in Settings (falls back to the defaults)."""
    chosen = (settings or {}).get("platforms") or {}
    return [d for d, _, on in PLATFORMS if chosen.get(d, on)]

def host_of(url):
    u = (url or "").strip()
    return urlparse(u if "//" in u else "https://" + u).netloc.lower().split(":")[0].removeprefix("www.")

def platform_of(url_or_host):
    """'acme.lovable.app' -> 'lovable.app' (None if it's not one of our platforms)."""
    h = host_of(url_or_host)
    for d in sorted(DOMAINS, key=len, reverse=True):
        if h.endswith("." + d):
            sub = h[: -len(d) - 1]
            if _LABEL_RE.match(sub) and sub not in RESERVED: return d
    return None

def site_host(url, only=None):
    """Normalise to 'name.platform' if it's a real site on one of the platforms (optionally only these domains)."""
    d = platform_of(url)
    if not d or (only and d not in only): return None
    return host_of(url)

def label_for(url):
    d = platform_of(url)
    return LABEL.get(d, "website") if d else "website"

def lead_key(url):
    """Stable id for a lead. Base44 keeps its old short ids; other platforms get 'name--platform'."""
    h = host_of(url)
    d = platform_of(h)
    if d == "base44.app": return h.split(".")[0]
    if d: return f"{h[: -len(d) - 1]}--{d.split('.')[0]}"
    return re.sub(r"[^a-z0-9]+", "-", h).strip("-")
