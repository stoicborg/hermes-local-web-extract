<div align="center">

# hermes-local-web-extract

**Read the web cheaply first. Escalate only when you have to. Walk away from walls.**

A [Hermes Agent](https://hermes-agent.nousresearch.com) `web_extract` backend that tries a fast local extractor,
then (optionally) a real browser, then (optionally) Tavily, and stops at human-verification walls instead of fighting them.

[![Hermes](https://img.shields.io/badge/Hermes-%E2%89%A5%200.23.0-8b5cf6?style=flat-square)](https://hermes-agent.nousresearch.com)
[![Tests](https://img.shields.io/badge/unit-60%20passed-10b981?style=flat-square)](#tests)
[![e2e](https://img.shields.io/badge/e2e-14%20passed-10b981?style=flat-square)](#tests)
[![License: MIT](https://img.shields.io/badge/license-MIT-64748b?style=flat-square)](LICENSE)

</div>

---

Most pages don't need a browser, and most agents pay for one (or for an API call) anyway. local-web-extract puts the
cheap path first:

```
 web_extract(urls)
      │
      ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ TIER 1  sidecar (Docker, 127.0.0.1)                          ~0.3 s, free │
 │   curl_cffi (Chrome TLS) → trafilatura → [< 800 chars?] → Readability.js  │
 │   PDFs → pypdf        private/loopback IPs refused on every redirect hop  │
 └──────────────┬──────────────────────────────────┬─────────────────────────┘
        good ≥ 800 chars                   short / blocked / error
        └──► return                                 │          verification wall seen?
                                                    │           └─► skip the browser, go to tier 3
                                                    ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ TIER 2  Camofox (optional, REST)             serial, 30 s, read-only probe│
 │   open tab → poll DOM until stable → read text → close tab                │
 │   probe sees a challenge page? → give up on this page immediately         │
 └──────────────┬────────────────────────────────────────────────────────────┘
        ≥ 300 chars  │            short / failed
        └──► return  ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ TIER 3  Tavily extract (optional, needs TAVILY_API_KEY)     batch, 30 s   │
 └──────────────┬────────────────────────────────────────────────────────────┘
                ▼
        best text we got (labelled "short"), or a per-URL error entry. Never an exception.
```

Tier 2 and tier 3 are **optional**. No `CAMOFOX_URL`, or Camofox doesn't answer `/health`? Tier 2 is skipped. No
`TAVILY_API_KEY`? Tier 3 is skipped. With neither, you still get tier 1, which handled 19 of 30 pages in the benchmark below.

## Design stance: it detects walls, it does not break them

When a page is a human-verification wall (DataDome, a Cloudflare challenge), local-web-extract **recognises it and gives
up on that page early**. That is deliberate and permanent:

- no captcha solving, no solver services, no OCR of puzzles;
- no clicking, dragging or typing into verification widgets;
- tier 2 only reads the DOM (`POST /tabs`, `POST /tabs/:id/evaluate`, `DELETE /tabs/:id`) and never calls
  click/type/press endpoints. A unit test greps the source to keep it that way.

The point of detection is to *stop wasting 30 seconds per page* and to tell the model plainly "this page is behind
a verification wall" so it can pick another source, not to get past it. If you need to defeat anti-bot systems,
this isn't the plugin, and PRs adding that will be declined.

## Requirements

- Hermes Agent ≥ 0.23.0
- Docker (tier 1 sidecar)
- Optional: a [camofox-browser](https://github.com/jo-inc/camofox-browser) server (tier 2), a Tavily API key (tier 3)

## Install

```bash
# 1. the plugin
hermes plugins install stoicborg/hermes-local-web-extract --enable

# 2. the tier-1 sidecar (builds a pinned image once, listens on 127.0.0.1:18650 only)
bash ~/.hermes/plugins/local-web-extract/sidecar/run.sh

# 3. point web_extract at it  (config.yaml)
#    web:
#      extract_backend: local-web-extract
```

Optional tiers, via the environment Hermes already reads (`~/.hermes/.env`):

```bash
CAMOFOX_URL=http://127.0.0.1:9377     # tier 2 (CAMOFOX_API_KEY too, if your server wants one)
TAVILY_API_KEY=tvly-...               # tier 3
```

Stop the sidecar with `bash .../sidecar/stop.sh`. To go back, set `web.extract_backend` to your previous value.
Hermes only loads backend plugins you have enabled, so `--enable` (or `hermes plugins enable local-web-extract`) is needed.

> The plugin repo is unpublished while this README is under review; until it is on GitHub, install from a local
> clone: `hermes plugins install file:///path/to/hermes-local-web-extract --enable`.

## Configuration

Every setting resolves **env var → `plugins.entries.local-web-extract.settings.<key>` → default**. Env names are
`LOCAL_WEB_EXTRACT_<KEY>` in upper case.

```yaml
plugins:
  entries:
    local-web-extract:
      settings:
        tiers: [sidecar, camofox, tavily]
        min_good_chars: 800
        camofox_timeout: 30
```

| Key | Default | Meaning |
|---|---|---|
| `tiers` | `[sidecar, camofox, tavily]` | Which tiers may run (order is fixed). `[sidecar]` = local only. |
| `sidecar_url` | `http://127.0.0.1:18650` | Tier-1 sidecar address. |
| `sidecar_timeout` | `40` | Seconds for one sidecar request (download 20 s + extraction). |
| `sidecar_workers` | `5` | Parallel sidecar requests per call. |
| `min_good_chars` | `800` | Tier-1 text shorter than this escalates to tier 2. |
| `camofox_url` | `""` → `$CAMOFOX_URL` | Empty everywhere = tier 2 skipped. |
| `camofox_timeout` | `30` | Seconds per page in tier 2. Pages are processed one at a time. |
| `min_camofox_chars` | `300` | Tier-2 text shorter than this counts as a failure. |
| `tavily_timeout` | `30` | Seconds for the batched Tavily call. |
| `tavily_base_url` | `https://api.tavily.com` | |
| `challenge_to_tavily` | `true` | After a wall is detected, still try Tavily (a plain API call, no browser). |
| `budget_s` | `110` | Whole-call budget; tier 2 is skipped when it would not leave room for tier 3 (Hermes' default `web.extract_timeout` is 120 s). |
| `log_enabled` / `log_path` | `true` / `<HERMES_HOME>/logs/local-web-extract.jsonl` | One JSON line per URL: tier, method, chars, seconds. Query strings are never logged. |

Sidecar knobs (environment of `run.sh`): `LOCAL_WEB_EXTRACT_PORT` (18650), `LOCAL_WEB_EXTRACT_BIND` (`127.0.0.1`),
`LOCAL_WEB_EXTRACT_NAME`, `LOCAL_WEB_EXTRACT_REBUILD=1`. The container runs as `nobody`, read-only root, all capabilities
dropped, 768 MB. Binding to anything but loopback exposes an unauthenticated fetch proxy: don't.

## Result format

Same shape as Hermes' built-in Tavily provider (`url, title, content, raw_content, metadata`); failures are
`{url, title:"", content:"", error, metadata}` entries, never exceptions. `metadata.tier` is `1:trafilatura`,
`1:readability`, `1:pypdf`, `2:camofox`, `3:tavily`, or `N:short` when every tier fell short and the best text is returned
anyway. (Hermes trims `metadata` before the model sees it; the tier log keeps it.) Walls produce an error like
`blocked by anti-bot challenge (DataDome/Cloudflare): detected DataDome at tier 1 …`.

## Benchmark (home Raspberry Pi 5, 30 real URLs, one run)

| | pages usable | median time |
|---|---|---|
| Tier 1 only (trafilatura or Readability) | **19 / 30** | ~0.3 s |
| Tier 1 + tier 2 (Camofox) | **26 / 30** | tier 2: ~8 s median |
| Tavily extract, strict criterion | 17 / 30 | ~1.4 s (per 5-URL batch, not per URL) |
| Tavily extract, "returned anything" | 25 / 30 | |

"Usable" = ≥ 800 chars **and** ≥ 2 of 3 key sentences from the page found in the output. This is an automatic proxy: it
penalises Tavily's whole-page Markdown, so read the strict number as a floor. Tier 1 + tier 2 together were usable on 10
pages where Tavily was not (e.g. BBC, The Guardian, Reddit, REI, Wikipedia-JA, YouTube); Tavily was usable on HN, where the
local tiers were not. Every method failed on Amazon, Yelp, g2 (Cloudflare) and one gear-review site. Tier-1 timings are
the sidecar's own; Tavily's are per 5-URL batch, so the two columns are not like-for-like.
**Single run, 30 hand-picked pages, one network location: indicative, not a study.**

## Tests

```bash
bash tests/run_unit.sh                                   # 60 unit tests, mocked, <1 s, clean python container
E2E_CAMOFOX_IMAGE=<camofox-browser image> bash tests/e2e/run.sh   # real pages, clean Hermes in Docker
```

Unit: ladder logic, optional-tier skipping, error format, batches, settings precedence, wall detection, private-IP guard,
tier-2 against a scripted Camofox, tier-3 against a mocked Tavily, "no click endpoints" source check.

E2E (clean Hermes v0.23.0 container, isolated network, real internet, 14 checks): install + enable + `plugins validate`;
Wikipedia → tier 1; an arXiv PDF → tier 1 (pypdf); mixed batch with a private IP and a dead host keeps order; a JS-heavy
page (36kr) falls to tier 2 on a real Camofox; Yelp (DataDome) fails cleanly in ~10 s with a clear error; Glassdoor serves a
real Cloudflare challenge to plain HTTP clients and tier 1 flags it; tier 2's probe exits early on a real DataDome page (g2).
Tavily is **not** exercised end-to-end (no key in CI); it is covered by mocks only.

## Limitations

- **Tier 2's Cloudflare branch is verified on a synthetic page only.** Camofox's own browser fingerprint was usually served
  real content by Cloudflare sites (Glassdoor, 36kr), so no live Cloudflare challenge ever reached the probe; the DataDome
  branch was verified live (g2), the Cloudflare branch against a local page that mimics its markup.
- Walls are detected by markers, not by a 403 alone; a new vendor, or a changed markup, shows up as an ordinary failure.
- Sites that need login, or content rendered only after interaction, are out of scope.
- Camofox is processed serially (one real browser). Zhihu-style pages that never settle burn the full 30 s.
- The Readability pass runs on the same downloaded HTML as trafilatura; it does not execute page scripts.
- Tavily results go to a third party; the sidecar and Camofox keep everything local.
- Tested against Hermes 0.23.0 only.

## Licenses

MIT. Third-party packages used by the sidecar image and their licenses are listed in [NOTICE](NOTICE).
