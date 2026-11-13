"""Unit tests for tier 2 (Camofox REST, mocked), tier 3 (Tavily, mocked), the sidecar's challenge detector and SSRF guard."""
import json

import pytest

from helpers import T, Settings, el


# ── challenge detection (sidecar) ───────────────────────────────────────────
CF_PAGE = b"<html><head><title>Just a moment...</title></head><body><script src='https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1'></script></body></html>"
DD_PAGE = b"<html><body><iframe src='https://geo.captcha-delivery.com/captcha/?initialCid=abc'></iframe></body></html>"
REGULAR = b"<html><title>Hello</title><body>" + b"<p>just a moment of your time</p>" * 5 + b"</body></html>"


def test_detect_cloudflare_markup(sidecar_app):
    assert sidecar_app.detect_challenge(403, {}, CF_PAGE) == "Cloudflare"


def test_detect_cloudflare_header(sidecar_app):
    assert sidecar_app.detect_challenge(403, {"cf-mitigated": "challenge"}, b"x") == "Cloudflare"


def test_detect_datadome(sidecar_app):
    assert sidecar_app.detect_challenge(403, {}, DD_PAGE) == "DataDome"
    assert sidecar_app.detect_challenge(403, {"Server": "DataDome"}, b"blocked") == "DataDome"


def test_plain_403_is_not_a_challenge(sidecar_app):
    assert sidecar_app.detect_challenge(403, {"server": "AkamaiGHost"}, b"<html>Access Denied</html>") is None


def test_normal_page_mentioning_phrase_not_flagged(sidecar_app):
    assert sidecar_app.detect_challenge(200, {}, REGULAR) is None


def test_large_200_page_not_scanned_as_challenge(sidecar_app):
    big = b"<p>captcha-delivery.com</p>" + b"a" * 40_000
    assert sidecar_app.detect_challenge(200, {}, big) is None


def test_do_extract_reports_challenge_and_stops(sidecar_app, monkeypatch):
    class R:
        status_code, headers, content, text = 403, {}, CF_PAGE, ""
    monkeypatch.setattr(sidecar_app, "fetch", lambda url: (R(), url))
    import types, sys
    sys.modules.setdefault("trafilatura", types.SimpleNamespace())  # not reached for challenge pages
    res = sidecar_app.do_extract("https://x.test/")
    assert (res["ok"], res["error_kind"], res["challenge"]) == (False, "challenge", "Cloudflare")


@pytest.mark.parametrize("url,kind", [("http://127.0.0.1/", "blocked_ip"), ("http://[::1]/", "blocked_ip"),
                                      ("http://169.254.169.254/latest", "blocked_ip"), ("http://10.1.2.3/", "blocked_ip"),
                                      ("http://0.0.0.0/", "blocked_ip"),
                                      ("file:///etc/passwd", "other"), ("ftp://example.com/", "other")])
def test_ssrf_guard(sidecar_app, url, kind):
    r = sidecar_app.check_public(url)
    assert r is not None and r[0] == kind


def test_ssrf_guard_allows_public_ip(sidecar_app):
    assert sidecar_app.check_public("http://93.184.216.34/") is None


# ── tier 2 against a mocked camofox REST API ────────────────────────────────
class FakeCamofox:
    """Stands in for CamofoxClient: scripted evaluate() answers."""

    def __init__(self, challenge="", text="body text " * 100, title="Page"):
        self.challenge, self.text, self.title = challenge, text, title
        self.closed, self.evals = [], 0

    def open(self, url, timeout):
        return "tab1"

    def eval(self, tab, expr, timeout=15):
        self.evals += 1
        if expr == T.CHALLENGE_JS:
            return self.challenge
        if expr == T.STATE_JS:
            return json.dumps(["complete", self.title, len(self.text)])
        if expr == T.CONTENT_JS:
            return json.dumps(["https://final.test/", self.title, self.text])
        raise AssertionError("unexpected expression")

    def close(self, tab):
        self.closed.append(tab)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(T.time, "sleep", lambda s: None)


def test_tier2_success_and_tab_closed():
    fc = FakeCamofox()
    r = T.tier2("https://a.test/", Settings(camofox_url="http://c"), client=fc)
    assert r["ok"] and r["method"] == "camofox" and r["final_url"] == "https://final.test/" and fc.closed == ["tab1"]


def test_tier2_too_short_is_failure(monkeypatch):
    t = {"now": 1000.0}

    def fake_time():   # each call advances 10 s, so the 30 s polling window closes after a few polls
        t["now"] += 10
        return t["now"]
    monkeypatch.setattr(T.time, "time", fake_time)
    r = T.tier2("https://a.test/", Settings(camofox_url="http://c"), client=FakeCamofox(text="tiny " * 5))
    assert not r["ok"] and "only" in r["error"]


def test_tier2_datadome_early_exit_reads_nothing_else():
    fc = FakeCamofox(challenge="DataDome")
    r = T.tier2("https://a.test/", Settings(camofox_url="http://c"), client=fc)
    assert (r["ok"], r["error_kind"], r["challenge"]) == (False, "challenge", "DataDome")
    assert fc.closed == ["tab1"] and fc.evals <= 2   # probe, then out: no content read, no interaction


def test_tier2_cloudflare_needs_two_sightings_then_exits():
    fc = FakeCamofox(challenge="Cloudflare")
    r = T.tier2("https://a.test/", Settings(camofox_url="http://c"), client=fc)
    assert r["challenge"] == "Cloudflare" and r["error_kind"] == "challenge"


def test_tier2_only_uses_read_only_endpoints():
    """The Camofox client may only create/evaluate/close tabs. No click/type/press endpoints are ever used."""
    import inspect
    src = inspect.getsource(T)
    for banned in ("/click", "/type", "/press", "/scroll", "captcha_solver", "2captcha", "capsolver", "anticaptcha"):
        assert banned not in src.replace("captcha-delivery.com", "")


def test_tier2_open_failure_is_clean_error():
    class Broken(FakeCamofox):
        def open(self, url, timeout):
            raise ConnectionError("refused")
    r = T.tier2("https://a.test/", Settings(camofox_url="http://c"), client=Broken())
    assert not r["ok"] and "ConnectionError" in r["error"]


def test_camofox_health_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(T.CamofoxClient, "health", lambda self: calls.append(1) or True)
    cfg = Settings(camofox_url="http://c")
    assert el.camofox_usable(cfg) and el.camofox_usable(cfg) and len(calls) == 1


# ── tier 3 against a mocked Tavily API ──────────────────────────────────────
def test_tier3_without_key_is_skipped():
    out = T.tier3(["https://a.test/"], Settings())
    assert out["https://a.test/"]["error_kind"] == "skipped" and "TAVILY_API_KEY" in out["https://a.test/"]["error"]


def test_tier3_parses_results_and_failed(monkeypatch):
    data = {"results": [{"url": "https://a.test", "raw_content": "hello " * 50, "title": "A"}],
            "failed_results": [{"url": "https://b.test/", "error": "Failed to fetch url"}]}
    monkeypatch.setattr(T, "_http_json", lambda *a, **k: (200, data))
    out = T.tier3(["https://a.test/", "https://b.test/", "https://c.test/"], Settings(tavily_api_key="k"))
    assert out["https://a.test/"]["ok"] and out["https://a.test/"]["title"] == "A"   # trailing-slash tolerant
    assert out["https://b.test/"]["error"] == "tavily: Failed to fetch url"
    assert out["https://c.test/"]["error"] == "tavily: no result for this URL"


def test_tier3_http_error_and_exception(monkeypatch):
    monkeypatch.setattr(T, "_http_json", lambda *a, **k: (401, {"detail": "bad key"}))
    assert "bad key" in T.tier3(["https://a.test/"], Settings(tavily_api_key="k"))["https://a.test/"]["error"]

    def boom(*a, **k):
        raise TimeoutError("slow")
    monkeypatch.setattr(T, "_http_json", boom)
    assert "TimeoutError" in T.tier3(["https://a.test/"], Settings(tavily_api_key="k"))["https://a.test/"]["error"]


def test_tier3_request_shape(monkeypatch):
    seen = {}
    monkeypatch.setattr(T, "_http_json", lambda m, url, body=None, headers=None, timeout=30: seen.update(url=url, body=body, h=headers, t=timeout) or (200, {"results": []}))
    T.tier3(["https://a.test/"], Settings(tavily_api_key="k", tavily_timeout=7))
    assert seen["url"].endswith("/extract") and seen["body"] == {"urls": ["https://a.test/"], "include_images": False}
    assert seen["h"]["Authorization"] == "Bearer k" and seen["t"] == 7


def test_tier1_sidecar_down(monkeypatch):
    def boom(*a, **k):
        raise ConnectionRefusedError("no")
    monkeypatch.setattr(T, "_http_json", boom)
    r = T.tier1("https://a.test/", Settings())
    assert not r["ok"] and r["error_kind"] == "sidecar"
