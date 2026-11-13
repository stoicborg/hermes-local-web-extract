"""Settings resolution for local-web-extract.

Precedence for every key: environment variable (LOCAL_WEB_EXTRACT_<KEY>) > plugin settings in
config.yaml (plugins.entries.local-web-extract.settings.<key>) > built-in default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

ALL_TIERS = ("sidecar", "camofox", "tavily")

DEFAULTS: dict = {
    "tiers": list(ALL_TIERS),            # which tiers may run (order is fixed: sidecar -> camofox -> tavily)
    "sidecar_url": "http://127.0.0.1:18650",
    "sidecar_timeout": 40,               # seconds, whole sidecar request (download + extraction)
    "sidecar_workers": 5,                # parallel sidecar requests per web_extract call
    "min_good_chars": 800,               # tier 1 result shorter than this escalates to tier 2
    "camofox_url": "",                   # empty -> $CAMOFOX_URL -> tier skipped
    "camofox_timeout": 30,
    "min_camofox_chars": 300,            # tier 2 result shorter than this counts as a failure
    "tavily_timeout": 30,
    "tavily_base_url": "https://api.tavily.com",
    "challenge_to_tavily": True,         # a detected challenge page still goes to tier 3 (a plain API call)
    "budget_s": 110.0,                   # whole-call time budget (Hermes web.extract_timeout defaults to 120)
    "log_enabled": True,
    "log_path": "",                      # empty -> <HERMES_HOME>/logs/local-web-extract.jsonl
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _secret(name: str) -> str:
    """Config-aware env lookup (Hermes .env aware when available), else os.environ."""
    try:
        from agent.web_search_provider import get_provider_env  # type: ignore
        return get_provider_env(name)
    except Exception:
        return (os.environ.get(name) or "").strip()


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _coerce(key: str, value: Any, default: Any) -> Any:
    """Coerce to the default's type; fall back to the default on garbage (never raise)."""
    try:
        if key == "tiers":
            if isinstance(value, str):
                value = [x.strip() for x in value.split(",")]
            tiers = [str(x).strip().lower() for x in (value or [])]
            return [t for t in ALL_TIERS if t in tiers]
        if isinstance(default, bool):
            if isinstance(value, str):
                v = value.strip().lower()
                if v in _TRUE:
                    return True
                if v in _FALSE:
                    return False
                return default
            return bool(value)
        if isinstance(default, int):
            return max(1, int(float(value)))
        if isinstance(default, float):
            return max(1.0, float(value))
        return str(value).strip()
    except Exception:
        return default


@dataclass
class Settings:
    tiers: List[str] = field(default_factory=lambda: list(ALL_TIERS))
    sidecar_url: str = DEFAULTS["sidecar_url"]
    sidecar_timeout: int = DEFAULTS["sidecar_timeout"]
    sidecar_workers: int = DEFAULTS["sidecar_workers"]
    min_good_chars: int = DEFAULTS["min_good_chars"]
    camofox_url: str = ""
    camofox_api_key: str = ""
    camofox_timeout: int = DEFAULTS["camofox_timeout"]
    min_camofox_chars: int = DEFAULTS["min_camofox_chars"]
    tavily_api_key: str = ""
    tavily_base_url: str = DEFAULTS["tavily_base_url"]
    tavily_timeout: int = DEFAULTS["tavily_timeout"]
    challenge_to_tavily: bool = True
    budget_s: float = DEFAULTS["budget_s"]
    log_enabled: bool = True
    log_path: str = ""

    def enabled(self, tier: str) -> bool:
        return tier in self.tiers

    def resolved_log_path(self) -> Path:
        return Path(self.log_path) if self.log_path else hermes_home() / "logs" / "local-web-extract.jsonl"


def load_settings(get_config: Optional[Callable[[str, Any], Any]] = None) -> Settings:
    """Build Settings. `get_config(key, default)` is ctx.get_config (plugin settings); may be None/raise."""
    vals: dict = {}
    for key, default in DEFAULTS.items():
        raw: Any = default
        if get_config is not None:
            try:
                raw = get_config(key, default)
            except Exception:
                raw = default
        env = os.environ.get("LOCAL_WEB_EXTRACT_" + key.upper())
        if env is not None and env != "":
            raw = env
        vals[key] = _coerce(key, raw, default) if raw is not None else default
    # Optional third-party tiers read their well-known env vars (kept out of config.yaml on purpose).
    camofox_url = (vals["camofox_url"] or _secret("CAMOFOX_URL")).rstrip("/")
    return Settings(
        camofox_url=camofox_url,
        camofox_api_key=_secret("CAMOFOX_API_KEY"),
        tavily_api_key=_secret("TAVILY_API_KEY"),
        **{k: (v.rstrip("/") if k in ("sidecar_url", "tavily_base_url") else v)
           for k, v in vals.items() if k != "camofox_url"},
    )
