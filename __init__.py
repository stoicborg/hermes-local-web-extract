"""local-web-extract: a tiered `web_extract` backend for Hermes Agent.

  tier 1  local sidecar   (curl_cffi + trafilatura / Readability / pypdf)   fast, free, no browser
  tier 2  Camofox         (optional, REST)                                    real browser for JS-heavy pages
  tier 3  Tavily extract  (optional, API key)                                 hosted fallback

Select with `web.extract_backend: local-web-extract` (and `hermes plugins enable local-web-extract`).

Design stance: verification walls (DataDome / Cloudflare challenges, captchas) are DETECTED and the
page is abandoned early. This plugin contains no captcha solving, no solver services and no
clicking/dragging of verification widgets, and never will.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from . import tiers as T
from .settings import Settings, load_settings

try:  # real Hermes
    from agent.web_search_provider import WebSearchProvider
except Exception:  # unit tests outside Hermes
    class WebSearchProvider:  # type: ignore[no-redef]
        pass

logger = logging.getLogger("tools.web_tools")  # same logger as web_extract, one place to look

NAME = "local-web-extract"
_NO_ESCALATE = {"dns", "blocked_ip"}  # escalating cannot help: unknown host / private address refused

_health_cache: Dict[str, tuple] = {}   # camofox_url -> (checked_at, ok)
_HEALTH_TTL = 30.0


# ── helpers ──────────────────────────────────────────────────────────────────
def _safe_url(u: str) -> str:
    p = urllib.parse.urlsplit(u)
    return f"{p.scheme}://{p.netloc}{p.path}"[:200]  # query strings may carry secrets: never log them


def _log_tier(cfg: Settings, rec: dict) -> None:
    logger.info("local-web-extract: %s tier=%s method=%s chars=%s %.1fs%s", rec["url"], rec["tier"], rec.get("method"),
                rec.get("chars"), rec.get("secs", 0), f" err={rec['error']}" if rec.get("error") else "")
    if not cfg.log_enabled:
        return
    try:
        path = cfg.resolved_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), **rec}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def camofox_usable(cfg: Settings, client: Optional[T.CamofoxClient] = None) -> bool:
    """Tier 2 runs only when enabled, a URL is configured and /health answers (cached 30 s)."""
    if not cfg.enabled("camofox") or not cfg.camofox_url:
        return False
    now = time.time()
    hit = _health_cache.get(cfg.camofox_url)
    if hit and now - hit[0] < _HEALTH_TTL:
        return hit[1]
    ok = (client or T.CamofoxClient(cfg)).health()
    _health_cache[cfg.camofox_url] = (now, ok)
    return ok


def tavily_usable(cfg: Settings) -> bool:
    return cfg.enabled("tavily") and bool(cfg.tavily_api_key)


def _entry(url: str, res: dict, tier: str) -> dict:
    text = res.get("text", "")
    title = res.get("title", "")
    return {"url": url, "title": title, "content": text, "raw_content": text,
            "metadata": {"sourceURL": url, "title": title, "tier": tier, "method": res.get("method", ""),
                         "final_url": res.get("final_url") or url}}


def _error_entry(url: str, error: str, tier: str) -> dict:
    return {"url": url, "title": "", "content": "", "error": error, "metadata": {"sourceURL": url, "tier": tier}}


# ── the ladder ───────────────────────────────────────────────────────────────
def run_ladder(urls: List[str], cfg: Settings, *, t1=None, t2=None, t3=None) -> List[dict]:
    """Extract `urls`. The tier callables are injectable for tests."""
    t1 = t1 or (lambda u: T.tier1(u, cfg))
    t2 = t2 or (lambda u: T.tier2(u, cfg))
    t3 = t3 or (lambda us: T.tier3(us, cfg))
    t_start = time.time()
    urls = list(urls)
    best: Dict[str, dict] = {}
    errors: Dict[str, str] = {u: "" for u in urls}
    done: Dict[str, dict] = {}
    chal: Dict[str, tuple] = {}
    need2: List[str] = []
    need3: List[str] = []

    def remaining() -> float:
        return cfg.budget_s - (time.time() - t_start)

    def consider(u: str, res: dict, tier: str) -> None:
        if res.get("text") and len(res["text"]) > len(best.get(u, {}).get("text", "")):
            best[u] = {**res, "tier": tier}

    def add_err(u: str, msg: str) -> None:
        errors[u] = (errors[u] + " | " + msg) if errors[u] else msg

    use2 = camofox_usable(cfg)
    use3 = tavily_usable(cfg)

    # tier 1 (parallel)
    if cfg.enabled("sidecar"):
        with cf.ThreadPoolExecutor(max_workers=min(cfg.sidecar_workers, max(1, len(urls)))) as pool:
            r1 = dict(zip(urls, pool.map(t1, urls)))
    else:
        r1 = {u: {"ok": False, "text": "", "error": "sidecar tier disabled", "error_kind": "skipped"} for u in urls}
    for u in urls:
        r = r1[u]
        consider(u, r, "1")
        if r.get("ok") and len(r.get("text", "")) >= cfg.min_good_chars:
            done[u] = _entry(u, r, "1:" + (r.get("method") or "local"))
            _log_tier(cfg, {"url": _safe_url(u), "tier": "1", "method": r.get("method"), "chars": r.get("chars"), "secs": r.get("secs", 0)})
        elif r.get("error_kind") == "challenge":      # a wall is a wall: skip the browser, optionally ask tier 3
            chal[u] = (r.get("challenge") or "unknown", "1")
            add_err(u, r.get("error") or "blocked by anti-bot challenge")
            if cfg.challenge_to_tavily:
                need3.append(u)
        elif r.get("error_kind") in _NO_ESCALATE or r.get("status") in (404, 410):
            done[u] = _error_entry(u, r.get("error") or "failed", "1")
            _log_tier(cfg, {"url": _safe_url(u), "tier": "1-fail", "error": r.get("error"), "secs": r.get("secs", 0)})
        else:
            add_err(u, r.get("error") or f"tier1 short ({r.get('chars', 0)} chars)")
            need2.append(u)

    # tier 2 (serial, optional)
    for u in need2:
        if not use2:
            add_err(u, "camofox skipped (not configured or unreachable)")
            need3.append(u)
            continue
        if remaining() < cfg.camofox_timeout + cfg.tavily_timeout + 5:
            add_err(u, "camofox skipped (time budget)")
            need3.append(u)
            continue
        r = t2(u)
        consider(u, r, "2")
        if r.get("ok"):
            done[u] = _entry(u, r, "2:camofox")
            _log_tier(cfg, {"url": _safe_url(u), "tier": "2", "method": "camofox", "chars": r.get("chars"), "secs": r.get("secs", 0)})
        else:
            if r.get("error_kind") == "challenge":
                chal[u] = (r.get("challenge") or "unknown", "2")
            add_err(u, r.get("error") or "camofox failed")
            if not (r.get("error_kind") == "challenge" and not cfg.challenge_to_tavily):
                need3.append(u)

    # tier 3 (one batch, optional)
    if need3:
        if not use3:
            for u in need3:
                add_err(u, "tavily skipped (disabled or TAVILY_API_KEY not set)")
        else:
            r3 = ({u: {"ok": False, "text": "", "error": "tavily skipped (time budget)"} for u in need3}
                  if remaining() < 8 else t3(need3))
            for u in need3:
                r = r3[u]
                consider(u, r, "3")
                if r.get("ok"):
                    done[u] = _entry(u, r, "3:tavily")
                    extra = {"challenge": chal[u][0], "challenge_tier": chal[u][1]} if u in chal else {}
                    _log_tier(cfg, {"url": _safe_url(u), "tier": "3", "method": "tavily", "chars": r.get("chars"), "secs": r.get("secs", 0), **extra})
                else:
                    add_err(u, r.get("error") or "tavily failed")

    out: List[dict] = []
    for u in urls:
        if u in done:
            out.append(done[u])
            continue
        b = best.get(u)
        if b and b.get("text"):  # nothing was "good", but we do have some text: return it, labelled short
            out.append(_entry(u, b, f"{b['tier']}:short"))
            _log_tier(cfg, {"url": _safe_url(u), "tier": f"{b['tier']}-short", "method": b.get("method"), "chars": len(b["text"]), "error": errors[u][:200]})
        elif u in chal:
            msg = f"blocked by anti-bot challenge (DataDome/Cloudflare): detected {chal[u][0]} at tier {chal[u][1]}; {errors[u]}"
            _log_tier(cfg, {"url": _safe_url(u), "tier": "fail-challenge", "challenge": chal[u][0], "challenge_tier": chal[u][1], "error": msg[:300]})
            out.append(_error_entry(u, msg, "fail"))
        else:
            msg = "All extract tiers failed: " + (errors[u] or "unknown")
            _log_tier(cfg, {"url": _safe_url(u), "tier": "fail", "error": msg[:300]})
            out.append(_error_entry(u, msg, "fail"))
    return out


# ── Hermes provider ──────────────────────────────────────────────────────────
class LocalWebExtractProvider(WebSearchProvider):
    """Extract-only provider; `web_search` is never routed here."""

    def __init__(self, get_config=None):
        self._get_config = get_config

    @property
    def name(self) -> str:
        return NAME

    @property
    def display_name(self) -> str:
        return "Local Web Extract (sidecar -> Camofox -> Tavily)"

    def is_available(self) -> bool:
        return True  # cheap check, no network: missing tiers degrade gracefully

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def get_setup_schema(self) -> dict:
        return {"name": "Local Web Extract", "badge": "local", "tag": "sidecar -> Camofox -> Tavily", "env_vars": []}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        cfg = load_settings(self._get_config)
        try:
            return run_ladder(urls, cfg)
        except Exception as exc:  # never raise into web_extract: per-URL error entries instead
            logger.warning("local-web-extract: unexpected failure: %s: %s", type(exc).__name__, exc)
            return [_error_entry(u, f"local-web-extract internal error: {type(exc).__name__}: {exc}"[:300], "fail") for u in urls]


def register(ctx) -> None:
    get_config = getattr(ctx, "get_config", None)
    ctx.register_web_search_provider(LocalWebExtractProvider(get_config))
