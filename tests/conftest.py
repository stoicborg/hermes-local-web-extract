import importlib.util
import os

import pytest

from helpers import SIDECAR, T, Settings, el


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("LOCAL_WEB_EXTRACT_") or k in ("CAMOFOX_URL", "CAMOFOX_API_KEY", "TAVILY_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    el._health_cache.clear()
    monkeypatch.setattr(T.CamofoxClient, "health", lambda self: True)   # no real network in unit tests
    yield


@pytest.fixture
def cfg(tmp_path):
    """Settings with every tier usable and the log in tmp."""
    return Settings(camofox_url="http://camofox.test", tavily_api_key="test-key",
                    log_path=str(tmp_path / "ladder.jsonl"))


@pytest.fixture
def sidecar_app():
    spec = importlib.util.spec_from_file_location("sidecar_app", SIDECAR / "app.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
