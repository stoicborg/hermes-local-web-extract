"""The three tiers. Each returns a plain result dict:

    {"ok": bool, "text": str, "title": str, "method": str, "final_url": str, "chars": int,
     "secs": float, "error": str|None, "error_kind": str|None, "challenge": str|None}

Design stance: a verification wall (DataDome / Cloudflare challenge etc.) is only DETECTED, never
solved. No captcha solving, no solver services, no clicking or dragging of verification widgets.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .settings import Settings

CHALLENGE_NAMES = ("DataDome", "Cloudflare")


def _http_json(method: str, url: str, body: Optional[dict] = None, headers: Optional[dict] = None,
               timeout: float = 30) -> tuple[int, Any]:
    """Tiny JSON-over-HTTP helper (stdlib only). Returns (status, parsed_json_or_text). Never follows
    HTTP errors silently: 4xx/5xx come back as (status, body). Network errors raise."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def _fail(error: str, method: str = "", secs: float = 0.0, **extra: Any) -> dict:
    return {"ok": False, "text": "", "title": "", "method": method, "final_url": "", "chars": 0,
            "secs": secs, "error": error[:300], "error_kind": extra.pop("error_kind", None),
            "challenge": extra.pop("challenge", None), **extra}


# ── tier 1: sidecar ──────────────────────────────────────────────────────────
def tier1(url: str, cfg: Settings) -> dict:
    t0 = time.time()
    try:
        status, d = _http_json("POST", cfg.sidecar_url + "/extract", {"url": url}, timeout=cfg.sidecar_timeout)
        if status != 200 or not isinstance(d, dict):
            raise RuntimeError(f"HTTP {status}")
    except Exception as exc:  # sidecar down / timeout -> tier 1 is simply unavailable, ladder continues
        return _fail(f"sidecar unreachable: {type(exc).__name__}: {exc}", secs=time.time() - t0, error_kind="sidecar")
    d["secs"] = time.time() - t0
    d.setdefault("text", "")
    d.setdefault("title", "")
    return d


# ── tier 2: Camofox REST (optional) ──────────────────────────────────────────
# Read-only probe: is the current page a verification wall? It only reads the DOM. It never clicks,
# drags, injects, or tries to pass anything.
CHALLENGE_JS = r"""(()=>{
const t=(document.title||''), b=(document.body&&document.body.innerText)||'';
const n=b.length;
let dd=false; for(const f of document.querySelectorAll('iframe')){ if((f.src||'').includes('captcha-delivery.com')) dd=true; }
if(dd && n<400) return 'DataDome';
if(/captcha-delivery\.com/.test(location.hostname)) return 'DataDome';
const cf = /^(just a moment|attention required)/i.test(t) || !!document.querySelector('#challenge-form,#cf-challenge-running,#challenge-stage,.cf-turnstile,#cf-please-wait')
  || (n<600 && /(verify you are human|checking (if the site connection is secure|your browser))/i.test(b));
if(cf) return 'Cloudflare';
return '';
})()"""
STATE_JS = "JSON.stringify([document.readyState,document.title,(document.body&&document.body.innerText||'').length])"
CONTENT_JS = "JSON.stringify([location.href,document.title,document.body?document.body.innerText:''])"

_CAMOFOX_LOCK = threading.Lock()  # one page at a time: Camofox is a single real browser


class CamofoxClient:
    """Minimal client for the camofox-browser REST API (POST /tabs, POST /tabs/:id/evaluate, DELETE /tabs/:id)."""

    def __init__(self, cfg: Settings):
        self.base = cfg.camofox_url
        self.headers = {"Authorization": f"Bearer {cfg.camofox_api_key}"} if cfg.camofox_api_key else {}
        self.user = f"local_web_extract_{int(time.time() * 1000) % 10**9}"

    def health(self) -> bool:
        try:
            status, d = _http_json("GET", self.base + "/health", headers=self.headers, timeout=3)
            return status == 200
        except Exception:
            return False

    def open(self, url: str, timeout: float) -> str:
        status, d = _http_json("POST", self.base + "/tabs", {"userId": self.user, "listItemId": "local-web-extract", "url": url},
                               self.headers, timeout)
        if status >= 400 or not isinstance(d, dict) or not d.get("tabId"):
            raise RuntimeError(f"tab create failed: HTTP {status}")
        return d["tabId"]

    def eval(self, tab: str, expression: str, timeout: float = 15) -> Any:
        status, d = _http_json("POST", f"{self.base}/tabs/{quote(tab)}/evaluate",
                               {"userId": self.user, "expression": expression}, self.headers, timeout)
        if status >= 400 or not isinstance(d, dict) or not d.get("ok", True):
            raise RuntimeError(f"evaluate failed: HTTP {status}")
        return d.get("result")

    def close(self, tab: str) -> None:
        try:
            _http_json("DELETE", f"{self.base}/tabs/{quote(tab)}", {"userId": self.user}, self.headers, 10)
        except Exception:
            pass


def _as_str(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v)


def _camofox_work(url: str, cfg: Settings, client: CamofoxClient, t0: float, state: dict) -> Optional[tuple]:
    with _CAMOFOX_LOCK:
        tab = client.open(url, timeout=cfg.camofox_timeout)
        try:
            last, stable, seen = -1, 0, 0
            while time.time() - t0 < cfg.camofox_timeout:
                time.sleep(0.4)
                try:
                    ch = _as_str(client.eval(tab, CHALLENGE_JS) or "").strip().strip('"')
                    rs, title, n = json.loads(_as_str(client.eval(tab, STATE_JS)))
                except Exception:
                    continue
                if ch in CHALLENGE_NAMES:
                    seen += 1
                    if seen >= 2 or ch == "DataDome":   # Cloudflare needs two sightings to avoid a flash false-positive
                        state["challenge"] = ch
                        return None                      # early exit: never wait for / interact with the wall
                    continue
                seen = 0
                bad_title = any(x in title for x in ("Just a moment", "Verif", "Attention Required")) or n < 50
                stable = stable + 1 if (n == last and rs == "complete" and not bad_title) else 0
                last = n
                if stable >= 2:
                    break
            u, title, text = json.loads(_as_str(client.eval(tab, CONTENT_JS)))
            return u, title, text
        finally:
            client.close(tab)


def tier2(url: str, cfg: Settings, client: Optional[CamofoxClient] = None) -> dict:
    import concurrent.futures as cf
    t0 = time.time()
    client = client or CamofoxClient(cfg)
    state: dict = {}
    ex = cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(_camofox_work, url, cfg, client, t0, state)
    try:
        out = fut.result(timeout=cfg.camofox_timeout + 15)
    except Exception as exc:
        ex.shutdown(wait=False)
        return _fail(f"camofox: {type(exc).__name__}: {exc}", "camofox", time.time() - t0)
    ex.shutdown(wait=False)
    if out is None:
        ch = state.get("challenge", "")
        return _fail(f"blocked by anti-bot challenge ({ch}) at camofox", "camofox", time.time() - t0,
                     error_kind="challenge", challenge=ch)
    final, title, text = out
    text = (text or "").strip()
    ok = len(text) >= cfg.min_camofox_chars
    return {"ok": ok, "text": text, "title": title or "", "final_url": final, "method": "camofox", "chars": len(text),
            "secs": time.time() - t0, "error": None if ok else f"camofox returned only {len(text)} chars",
            "error_kind": None, "challenge": None}


# ── tier 3: Tavily extract (optional) ────────────────────────────────────────
def tier3(urls: List[str], cfg: Settings) -> Dict[str, dict]:
    if not cfg.tavily_api_key:
        return {u: _fail("TAVILY_API_KEY is not set", "tavily", error_kind="skipped") for u in urls}
    t0 = time.time()
    try:
        status, data = _http_json("POST", cfg.tavily_base_url + "/extract", {"urls": urls, "include_images": False},
                                  {"X-Client-Name": "hermes-agent", "Authorization": f"Bearer {cfg.tavily_api_key}"},
                                  cfg.tavily_timeout)
    except Exception as exc:
        return {u: _fail(f"tavily: {type(exc).__name__}: {exc}", "tavily") for u in urls}
    if status >= 400 or not isinstance(data, dict):
        msg = (json.dumps(data) if not isinstance(data, str) else data).strip()[:200] or f"HTTP {status}"
        return {u: _fail(f"tavily: {msg}", "tavily") for u in urls}
    secs = time.time() - t0
    out: Dict[str, dict] = {}
    results = data.get("results", []) or []
    for item in results:
        text = (item.get("raw_content") or item.get("content") or "").strip()
        src = item.get("url", "")
        match = next((u for u in urls if u == src or u.rstrip("/") == src.rstrip("/")), None)
        if match is None and len(urls) == len(results) == 1:
            match = urls[0]
        if match:
            out[match] = {"ok": bool(text), "text": text, "title": item.get("title", ""), "method": "tavily",
                          "final_url": match, "chars": len(text), "secs": secs,
                          "error": None if text else "tavily returned empty content", "error_kind": None, "challenge": None}
    for f in data.get("failed_results", []) or []:
        u = next((x for x in urls if x.rstrip("/") == str(f.get("url", "")).rstrip("/")), None)
        if u:
            out.setdefault(u, _fail("tavily: " + str(f.get("error", "extraction failed")), "tavily"))
    for u in urls:
        out.setdefault(u, _fail("tavily: no result for this URL", "tavily"))
    return out
