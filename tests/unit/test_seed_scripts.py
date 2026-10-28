"""The local seed scripts refuse non-local databases before connecting."""

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(filename: str):
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py").replace("-", "_"), _REPO_ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("filename", ["scripts-seed-local.py", "scripts-seed-demo-data.py"])
def test_refuses_a_redirected_database_url_without_connecting(monkeypatch, filename):
    from transcribe_api.runtime.settings_recording import get_settings

    script = _load_script(filename)
    monkeypatch.setattr(script, "create_engine", lambda *a, **k: pytest.fail("must not connect"))
    if hasattr(script, "_ensure_seed_audio"):
        monkeypatch.setattr(script, "_ensure_seed_audio", lambda: None)
    monkeypatch.setenv("DATABASE_CONNECTION_STRING", "postgresql://u:p@localhost/db?host=shared-db")
    get_settings.cache_clear()
    try:
        assert script.main() == 1
    finally:
        get_settings.cache_clear()
