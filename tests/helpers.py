"""Shared test helpers: load the plugin as package `local_web_extract` (repo root has __init__.py)."""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SIDECAR = ROOT / "sidecar"


def _load():
    if "local_web_extract" in sys.modules:
        return sys.modules["local_web_extract"]
    spec = importlib.util.spec_from_file_location("local_web_extract", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["local_web_extract"] = mod
    spec.loader.exec_module(mod)
    return mod


el = _load()
from local_web_extract import tiers as T  # noqa: E402
from local_web_extract.settings import Settings, load_settings  # noqa: E402


def good(n=2000, method="trafilatura", **kw):
    return {"ok": True, "text": "x" * n, "title": "T", "method": method, "chars": n, "secs": 0.1,
            "error": None, "error_kind": None, "challenge": None, **kw}


def bad(error="boom", kind=None, **kw):
    return {"ok": False, "text": "", "title": "", "method": "", "chars": 0, "secs": 0.1,
            "error": error, "error_kind": kind, "challenge": None, **kw}
