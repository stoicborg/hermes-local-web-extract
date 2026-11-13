"""Unit tests: tier logic, optional-tier skipping, error format, batches, settings. All network mocked."""
import json

import pytest

from helpers import good, bad, el, T, Settings, load_settings


def run(urls, cfg, t1=None, t2=None, t3=None):
    return el.run_ladder(urls, cfg,
                         t1=t1 or (lambda u: good()),
                         t2=t2 or (lambda u: pytest.fail("tier 2 must not run")),
                         t3=t3 or (lambda us: pytest.fail("tier 3 must not run")))


def test_tier1_good_stops_ladder(cfg):
    out = run(["https://a.test/x"], cfg)
    assert out[0]["metadata"]["tier"] == "1:trafilatura"
    assert out[0]["content"] == out[0]["raw_content"] and len(out[0]["content"]) == 2000
    assert set(out[0]) == {"url", "title", "content", "raw_content", "metadata"}  # same keys as built-in tavily


def test_short_tier1_escalates_to_camofox(cfg):
    out = run(["https://a.test/"], cfg, t1=lambda u: good(100), t2=lambda u: good(900, method="camofox"))
    assert out[0]["metadata"]["tier"] == "2:camofox"


def test_camofox_unconfigured_skips_to_tavily(cfg):
    cfg.camofox_url = ""
    out = run(["https://a.test/"], cfg, t1=lambda u: bad("HTTP 403 from origin", "http"),
              t3=lambda us: {u: good(500, method="tavily") for u in us})
    assert out[0]["metadata"]["tier"] == "3:tavily"


def test_camofox_unreachable_is_skipped(cfg, monkeypatch):
    monkeypatch.setattr(T.CamofoxClient, "health", lambda self: False)
    out = run(["https://a.test/"], cfg, t1=lambda u: bad("x", "http"),
              t3=lambda us: {u: good(500, method="tavily") for u in us})
    assert out[0]["metadata"]["tier"] == "3:tavily"


def test_no_tavily_key_skips_tier3_and_reports(cfg):
    cfg.tavily_api_key = ""
    cfg.camofox_url = ""
    out = run(["https://a.test/"], cfg, t1=lambda u: bad("HTTP 403 from origin", "http"))
    e = out[0]
    assert e["error"].startswith("All extract tiers failed")
    assert "camofox skipped" in e["error"] and "tavily skipped" in e["error"]
    assert e["content"] == "" and e["metadata"]["tier"] == "fail"


def test_tiers_setting_disables_tier(cfg, monkeypatch):
    cfg.tiers = ["sidecar"]
    monkeypatch.setattr(T.CamofoxClient, "health", lambda self: pytest.fail("must not probe a disabled tier"))
    out = run(["https://a.test/"], cfg, t1=lambda u: good(100))
    assert out[0]["metadata"]["tier"] == "1:short"   # best short text is still returned


def test_sidecar_tier_disabled(cfg):
    cfg.tiers = ["tavily"]
    cfg.camofox_url = ""
    out = run(["https://a.test/"], cfg, t1=lambda u: pytest.fail("sidecar disabled"),
              t3=lambda us: {u: good(500, method="tavily") for u in us})
    assert out[0]["metadata"]["tier"] == "3:tavily"


def test_sidecar_down_escalates(cfg):
    out = run(["https://a.test/"], cfg, t1=lambda u: bad("sidecar unreachable: URLError", "sidecar"),
              t2=lambda u: good(900, method="camofox"))
    assert out[0]["metadata"]["tier"] == "2:camofox"


@pytest.mark.parametrize("kind", ["dns", "blocked_ip"])
def test_no_escalation_for_dns_and_private(cfg, kind):
    out = run(["http://10.0.0.1/"], cfg, t1=lambda u: bad("nope", kind))
    assert out[0]["error"] == "nope" and out[0]["metadata"]["tier"] == "1"


def test_404_not_escalated(cfg):
    out = run(["https://a.test/"], cfg, t1=lambda u: bad("HTTP 404 from origin", "http", status=404))
    assert "404" in out[0]["error"]


def test_challenge_at_tier1_skips_camofox_goes_to_tavily(cfg):
    out = run(["https://a.test/"], cfg, t1=lambda u: bad("blocked by anti-bot challenge (DataDome)", "challenge", challenge="DataDome"),
              t3=lambda us: {u: good(600, method="tavily") for u in us})
    assert out[0]["metadata"]["tier"] == "3:tavily"


def test_challenge_everywhere_is_a_clean_failure(cfg):
    out = run(["https://a.test/"], cfg,
              t1=lambda u: bad("blocked by anti-bot challenge (Cloudflare)", "challenge", challenge="Cloudflare"),
              t3=lambda us: {u: bad("tavily: Failed to fetch url") for u in us})
    e = out[0]
    assert e["error"].startswith("blocked by anti-bot challenge") and "Cloudflare" in e["error"]
    assert e["content"] == ""


def test_challenge_to_tavily_off(cfg):
    cfg.challenge_to_tavily = False
    out = run(["https://a.test/"], cfg, t1=lambda u: bad("w", "challenge", challenge="Cloudflare"))
    assert "Cloudflare" in out[0]["error"]


def test_camofox_challenge_early_exit_recorded(cfg):
    out = run(["https://a.test/"], cfg, t1=lambda u: bad("x", "http"),
              t2=lambda u: bad("blocked by anti-bot challenge (DataDome) at camofox", "challenge", challenge="DataDome"),
              t3=lambda us: {u: bad("tavily: failed") for u in us})
    assert "DataDome" in out[0]["error"] and "tier 2" in out[0]["error"]


def test_batch_mixed_results_keep_order(cfg):
    urls = ["https://a.test/1", "https://a.test/2", "https://a.test/3", "https://a.test/4"]

    def t1(u):
        return {"https://a.test/1": good(), "https://a.test/2": good(50),
                "https://a.test/3": bad("HTTP 403", "http"), "https://a.test/4": bad("x", "dns")}[u]
    out = run(urls, cfg, t1=t1, t2=lambda u: good(900, method="camofox") if u.endswith("/2") else bad("c"),
              t3=lambda us: {u: good(700, method="tavily") for u in us})
    assert [e["url"] for e in out] == urls
    assert [e["metadata"]["tier"] for e in out] == ["1:trafilatura", "2:camofox", "3:tavily", "1"]


def test_tier3_called_once_as_batch(cfg):
    calls = []

    def t3(us):
        calls.append(list(us))
        return {u: good(500, method="tavily") for u in us}
    cfg.camofox_url = ""
    run(["https://a.test/1", "https://a.test/2"], cfg, t1=lambda u: bad("x", "http"), t3=t3)
    assert calls == [["https://a.test/1", "https://a.test/2"]]


def test_small_budget_skips_camofox_but_tries_tavily(cfg):
    cfg.budget_s = 50    # < camofox_timeout + tavily_timeout + 5
    out = run(["https://a.test/"], cfg, t1=lambda u: good(10),
              t3=lambda us: {u: good(500, method="tavily") for u in us})
    assert out[0]["metadata"]["tier"] == "3:tavily"


def test_exhausted_budget_skips_everything_and_returns_short_text(cfg):
    cfg.budget_s = 1
    out = run(["https://a.test/"], cfg, t1=lambda u: good(10))   # t2/t3 would fail the test if called
    assert out[0]["metadata"]["tier"] == "1:short" and len(out[0]["content"]) == 10


def test_empty_url_list(cfg):
    assert el.run_ladder([], cfg) == []


def test_error_entry_shape(cfg):
    cfg.camofox_url = ""
    cfg.tavily_api_key = ""
    e = run(["https://a.test/"], cfg, t1=lambda u: bad("x", "http"))[0]
    assert set(e) == {"url", "title", "content", "error", "metadata"}   # page_error shape + metadata


def test_provider_never_raises(monkeypatch, cfg):
    monkeypatch.setattr(el, "run_ladder", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")))
    out = el.LocalWebExtractProvider().extract(["https://a.test/"])
    assert out[0]["error"].startswith("local-web-extract internal error")


def test_provider_flags():
    p = el.LocalWebExtractProvider()
    assert (p.name, p.supports_extract(), p.supports_search(), p.is_available()) == ("local-web-extract", True, False, True)


def test_register_and_config_plumbing():
    seen = {}

    class Ctx:
        def get_config(self, k, d=None):
            return {"min_good_chars": 123}.get(k, d)

        def register_web_search_provider(self, p):
            seen["p"] = p
    el.register(Ctx())
    assert seen["p"].name == "local-web-extract"
    assert load_settings(seen["p"]._get_config).min_good_chars == 123


def test_logging_jsonl_has_no_query_string(cfg):
    run(["https://a.test/p?token=SECRET"], cfg)
    line = open(cfg.log_path).read()
    assert "SECRET" not in line and json.loads(line)["tier"] == "1"


def test_log_disabled(cfg, tmp_path):
    cfg.log_enabled = False
    run(["https://a.test/"], cfg)
    assert not (tmp_path / "ladder.jsonl").exists()


# ── settings ────────────────────────────────────────────────────────────────
def test_defaults():
    s = load_settings(None)
    assert s.tiers == ["sidecar", "camofox", "tavily"] and s.sidecar_url == "http://127.0.0.1:18650"
    assert (s.min_good_chars, s.camofox_timeout, s.tavily_timeout, s.camofox_url, s.tavily_api_key) == (800, 30, 30, "", "")


def test_env_beats_config_beats_default(monkeypatch):
    cfgget = lambda k, d=None: {"min_good_chars": 500, "sidecar_url": "http://cfg:1/"}.get(k, d)
    assert load_settings(cfgget).min_good_chars == 500 and load_settings(cfgget).sidecar_url == "http://cfg:1"
    monkeypatch.setenv("LOCAL_WEB_EXTRACT_MIN_GOOD_CHARS", "900")
    assert load_settings(cfgget).min_good_chars == 900


def test_garbage_values_fall_back(monkeypatch):
    monkeypatch.setenv("LOCAL_WEB_EXTRACT_MIN_GOOD_CHARS", "lots")
    monkeypatch.setenv("LOCAL_WEB_EXTRACT_LOG_ENABLED", "maybe")
    s = load_settings(None)
    assert s.min_good_chars == 800 and s.log_enabled is True


def test_tiers_parsing(monkeypatch):
    monkeypatch.setenv("LOCAL_WEB_EXTRACT_TIERS", "tavily, sidecar, bogus")
    assert load_settings(None).tiers == ["sidecar", "tavily"]


def test_camofox_and_tavily_env(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://127.0.0.1:9377/")
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    s = load_settings(None)
    assert s.camofox_url == "http://127.0.0.1:9377" and s.tavily_api_key == "k"


def test_get_config_raising_is_tolerated():
    def boom(k, d=None):
        raise ValueError("reserved")
    assert load_settings(boom).min_good_chars == 800


def test_no_tavily_check():
    s = Settings()
    assert not el.tavily_usable(s) and not el.camofox_usable(s)
