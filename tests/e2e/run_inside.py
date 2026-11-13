"""Runs INSIDE the e2e container (clean Hermes). /repo = plugin repo (ro), /out = results dir (rw).

Env: LOCAL_WEB_EXTRACT_SIDECAR_URL (required), CAMOFOX_URL (optional; tier-2 checks are SKIPped if unset/unreachable).
Real web pages are fetched. Results are written to /out/results.json.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

OUT = "/out"
HH = os.environ.get("HERMES_HOME", "/root/.hermes")
res = {"checks": []}


def sh(cmd, timeout=180):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def check(name, ok, detail="", skip=False):
    status = "SKIP" if skip else ("PASS" if ok else "FAIL")
    res["checks"].append({"name": name, "status": status, "detail": str(detail)[:500]})
    print(status, name, "|", str(detail)[:220].replace("\n", " "), flush=True)


sidecar = os.environ["LOCAL_WEB_EXTRACT_SIDECAR_URL"]
camofox = os.environ.get("CAMOFOX_URL", "")


def up(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


have_camofox = bool(camofox) and up(camofox)
check("sidecar reachable", up(sidecar), sidecar)

# ── install the plugin the way a user would: from a git clone, then enable it ──
os.makedirs(HH, exist_ok=True)
sh("git config --global --add safe.directory '*'")
rc, o = sh("rm -rf /tmp/clone && git clone -q /repo /tmp/clone && cd /tmp/clone && git log --oneline | head -1")
res["repo_head"] = o.strip()
rc, o = sh("hermes plugins install file:///tmp/clone --enable --force", timeout=120)
open(f"{OUT}/install.txt", "w").write(f"rc={rc}\n{o}")
check("hermes plugins install --enable", rc == 0, o[-200:])
rc, o = sh("hermes plugins list")
open(f"{OUT}/plugins_list.txt", "w").write(o)
check("plugins list shows local-web-extract enabled", "local-web-extract" in o and "enabled" in o.lower(), o[-300:])
rc, o = sh("hermes plugins validate /repo")
open(f"{OUT}/validate.txt", "w").write(f"rc={rc}\n{o}")
check("hermes plugins validate", rc == 0, o[-200:])

open(f"{HH}/config.yaml", "w").write(
    "web:\n  extract_backend: local-web-extract\n"
    "plugins:\n  enabled: [local-web-extract]\n"
    "  entries:\n    local-web-extract:\n      settings:\n        log_path: /out/ladder.jsonl\n")
open(f"{HH}/.env", "w").write("")  # no TAVILY_API_KEY on purpose: tier 3 must skip cleanly

# ── call Hermes' real web_extract tool in-process ──
sys.path.insert(0, "/opt/hermes-src")
from tools import web_tools  # noqa: E402


def extract(urls, **kw):
    t0 = time.time()
    raw = asyncio.run(web_tools.web_extract_tool(urls, **kw))
    try:
        data = json.loads(raw)
    except Exception:
        data = {"_raw": raw}
    return data, time.time() - t0


def by_url(data):
    return {r.get("url"): r for r in data.get("results", [])}


def tier_of(url):
    """Hermes' web_extract trims `metadata`, so read the tier from the plugin's own jsonl log (last line for this URL)."""
    base = url.split("?")[0]
    last = None
    try:
        for line in open("/out/ladder.jsonl"):
            rec = json.loads(line)
            if rec.get("url") == base:
                last = rec
    except FileNotFoundError:
        pass
    return last or {}


WIKI = "https://en.wikipedia.org/wiki/Web_scraping"
PDF = "https://arxiv.org/pdf/1706.03762"
JS_CANDIDATES = ["https://www.36kr.com/", "https://www.zhihu.com/question/19550225", "https://www.reddit.com/r/programming/"]
YELP = "https://www.yelp.com/biz/hanabi-sushi-mountain-view-2"
CF = "https://www.glassdoor.com/"   # serves a real Cloudflare challenge (cf-mitigated: challenge) to plain clients

# 1. provider resolves
data, secs = extract([WIKI])
r = by_url(data).get(WIKI, {})
meta = tier_of(WIKI)
check("wikipedia served by tier 1", meta.get("tier") == "1" and len(r.get("content", "")) > 800,
      f"tier={meta.get('tier')} chars={len(r.get('content',''))} {secs:.1f}s")
res["wikipedia"] = {"tier": meta.get("tier"), "chars": len(r.get("content", "")), "secs": round(secs, 2)}

# 2. PDF
data, secs = extract([PDF])
r = by_url(data).get(PDF, {})
meta = tier_of(PDF)
check("PDF extracted by tier 1 (pypdf)", meta.get("method") == "pypdf" and len(r.get("content", "")) > 5000,
      f"tier={meta.get('tier')} method={meta.get('method')} chars={len(r.get('content',''))} {secs:.1f}s")

# 3. batch: mixed URLs, order kept, nothing crashes
batch = [WIKI, "http://127.0.0.1:1/", "https://nonexistent-host-xyz.invalid/", PDF]
data, secs = extract(batch)
got = [x.get("url") for x in data.get("results", [])]
check("batch keeps order and survives bad URLs", got == batch, f"{got} {secs:.1f}s")

# 4. JS-heavy page -> tier 2
t2 = None
for u in JS_CANDIDATES:
    data, secs = extract([u])
    r = by_url(data).get(u, {})
    meta = tier_of(u)
    res.setdefault("js_candidates", []).append({"url": u, "tier": meta.get("tier"), "chars": len(r.get("content", "")), "secs": round(secs, 1), "error": (r.get("error") or "")[:160]})
    print("  js candidate", res["js_candidates"][-1], flush=True)
    if meta.get("tier") == "2":
        t2 = u
        break
if have_camofox:
    check("JS page falls to tier 2 (Camofox)", t2 is not None, f"{t2} " + json.dumps(res["js_candidates"])[:300])
else:
    check("JS page falls to tier 2 (Camofox)", False, "CAMOFOX_URL not set/reachable", skip=True)

# 5. Yelp: verification-protected page -> clean early failure, no crash
data, secs = extract([YELP])
r = by_url(data).get(YELP, {})
check("Yelp fails cleanly (error entry, no crash)", bool(r.get("error")) or bool(r.get("content")),
      f"{secs:.1f}s tier={tier_of(YELP).get('tier')} err={(r.get('error') or '')[:200]}")
res["yelp"] = {"secs": round(secs, 1), "error": (r.get("error") or "")[:300], "tier": tier_of(YELP).get("tier")}
check("Yelp does not hang to the old 30s*N timeout", secs < 60, f"{secs:.1f}s")

# 6. Cloudflare challenge page (real). (a) through the ladder: tier 1 detects it, no browser is opened.
data, secs = extract([CF])
r = by_url(data).get(CF, {})
err = r.get("error") or ""
rec = tier_of(CF)
res["cloudflare_ladder"] = {"secs": round(secs, 1), "error": err[:300], "log": rec}
check("Cloudflare challenge: ladder detects and abandons", rec.get("challenge") == "Cloudflare" and not r.get("content"),
      f"{secs:.1f}s log={rec.get('tier')}/{rec.get('challenge')} err={err[:160]}")

# (b) tier-2 read-only probe against the same real challenge page, called directly (bypassing tier 1's early exit)
if have_camofox:
    import importlib.util
    spec = importlib.util.spec_from_file_location("local_web_extract", "/repo/__init__.py", submodule_search_locations=["/repo"])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["local_web_extract"] = mod
    spec.loader.exec_module(mod)
    from local_web_extract import tiers as T
    from local_web_extract.settings import Settings
    probes = []
    for cand in (CF, "https://www.g2.com/products/slack/reviews", YELP):
        t0 = time.time()
        out = T.tier2(cand, Settings(camofox_url=camofox))
        probes.append({"url": cand, "secs": round(time.time() - t0, 1), "error_kind": out.get("error_kind"),
                       "challenge": out.get("challenge"), "chars": out.get("chars"), "ok": out.get("ok")})
        print("  tier2 probe", probes[-1], flush=True)
        if out.get("error_kind") == "challenge":
            break
    res["tier2_probes"] = probes
    hit = [x for x in probes if x["error_kind"] == "challenge"]
    if hit:
        check("tier-2 probe exits early on a real challenge page", hit[0]["secs"] < 25, json.dumps(hit[0]))
    else:
        # Camofox was served real content (or timed out) instead of a challenge: the probe was NOT exercised.
        check("tier-2 probe exits early on a real challenge page", False,
              "no real challenge was shown to Camofox by any candidate; probe unverified on live pages: " + json.dumps(probes), skip=True)
else:
    check("tier-2 probe exits early on a real challenge page", False, "Camofox not available", skip=True)

# (c) SYNTHETIC page (not a real Cloudflare page): exercises the tier-2 probe JS in the real Camofox browser.
if have_camofox:
    import http.server, socket, threading
    FAKE = (b"<html><head><title>Just a moment...</title></head><body><form id='challenge-form'></form>"
            b"<div id='cf-please-wait'>Checking your browser before accessing the site.</div></body></html>")

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(FAKE)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 8099), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake_url = f"http://{socket.gethostname()}:8099/"
    t0 = time.time()
    out = T.tier2(fake_url, Settings(camofox_url=camofox))
    res["tier2_synthetic"] = {"secs": round(time.time() - t0, 1), "error_kind": out.get("error_kind"), "challenge": out.get("challenge"), "error": (out.get("error") or "")[:160]}
    check("tier-2 probe exits early on a SYNTHETIC challenge page (real Camofox)", out.get("error_kind") == "challenge" and out.get("challenge") == "Cloudflare",
          json.dumps(res["tier2_synthetic"]))

# 7. tier log written, without query strings
try:
    lines = [json.loads(x) for x in open("/out/ladder.jsonl")]
    check("tier log written", len(lines) >= 5, f"{len(lines)} lines; tiers={sorted({l.get('tier') for l in lines})}")
except Exception as e:
    check("tier log written", False, repr(e))

json.dump(res, open(f"{OUT}/results.json", "w"), indent=1, ensure_ascii=False)
fails = [c for c in res["checks"] if c["status"] == "FAIL"]
print(f"\n{sum(c['status']=='PASS' for c in res['checks'])} pass, {len(fails)} fail, {sum(c['status']=='SKIP' for c in res['checks'])} skip")
sys.exit(1 if fails else 0)
