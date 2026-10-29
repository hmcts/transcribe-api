"""Conftest for the merged unit suite.

Scoped to tests/unit deliberately: tests/smoke and tests/functional run against
a deployed environment over HTTP and must not have a local settings environment
pinned underneath them.

Two problems arise from merging two suites into one process, and both are fixed
here rather than in either suite.

1. Memoised settings. Both `get_settings()` implementations are cached (`@cache`
   on the dictation side, `@lru_cache` on the recording side). Whichever suite
   ran first populated the cache and the other then saw the wrong settings, so
   results were order-dependent: dictation-first broke four recording auth
   tests, recording-first broke a dictation settings test.

2. Leakage from a developer's `.env`. Both settings classes declare
   `env_file=".env"`. Upstream the recording service had no `.env`, so its tests
   ran on declared defaults. This repo needs a `.env` to run the app locally,
   which silently changed test behaviour. Real environment variables take
   precedence over `env_file` in pydantic-settings, so setting them explicitly
   here makes the suite independent of whatever `.env` happens to contain.
"""

from __future__ import annotations

import os

import pytest

# A complete, inert test environment. Values are fake by design; nothing here
# should reach a real service.
TEST_ENV: dict[str, str] = {
    "ENVIRONMENT": "test",
    "APP_URL": "http://localhost:8000",
    "AZURE_AD_CLIENT_ID": "test-client-id",
    "AZURE_AD_TENANT_ID": "test-tenant-id",
    "AZURE_GROK_API_KEY": "test-grok-key",
    "AZURE_GROK_ENDPOINT": "https://test.openai.azure.com/",
    "AZURE_OPENAI_API_KEY": "test-openai-key",
    "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
    "AZURE_SPEECH_KEY": "test-speech-key",
    "AZURE_SPEECH_REGION": "eastus",
    "AZURE_SPEECH_ENDPOINT": "https://test.cognitiveservices.azure.com",
    "AZURE_STORAGE_ACCOUNT_NAME": "teststorage",
    "AZURE_STORAGE_CONTAINER_NAME": "test-container",
    "AZURE_STORAGE_TRANSCRIPTION_CONTAINER": "test-transcription-container",
    # Pinned so a developer's local .env (AUDIO_STORAGE_BACKEND=local,
    # SPEECH_BACKEND=mock — see runtime/settings_recording.py) can never
    # change what these tests actually exercise: real env vars outrank
    # env_file in pydantic-settings, but only for keys this dict sets.
    "AUDIO_STORAGE_BACKEND": "azure",
    "SPEECH_BACKEND": "azure",
    "LOCAL_AUDIO_BASE_URL": "",
    "DATABASE_CONNECTION_STRING": "postgresql://test:test@localhost:5432/test_db",
    "GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT": "{}",
    "GOV_NOTIFY_API_KEY": "test-notify-key",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-test-public-key-123456",
    "LANGFUSE_SECRET_KEY": "sk-lf-test-secret-key-123456",
    "LANGFUSE_HOST": "https://langfuse-ai.justice.gov.uk",
    "SENTRY_DSN": "",
    "JWT_SECRET_KEY": "test-jwt-secret",
    "DISABLE_FASTAPI_INSTRUMENTATION": "true",
    "RUN_WORKERS": "false",
}


# Pin the environment at IMPORT time as well as per test.
#
# The autouse fixture below runs before each test, which is too late for some
# modules: several import code that calls get_settings() at module level, so
# settings are built during COLLECTION, before any fixture runs. On a developer
# machine that is masked by .env (LocalSettings reads it); on the CNP Jenkins
# agent there is no .env, and collection failed with "7 validation errors for
# LocalSettings" — exactly the seven required dictation fields.
#
# pytest imports a conftest.py before collecting the modules beside it, so
# setting the variables here guarantees they exist for every collection-time
# import. Real environment variables outrank env_file in pydantic-settings, so
# this also keeps the suite independent of whatever a local .env contains.
os.environ.update(TEST_ENV)

# TEST_ENV only protects keys it lists — a test that does
# monkeypatch.delenv("SOME_KEY") to exercise the "not configured" branch (e.g.
# test_local_storage.py's test_raises_when_base_url_not_configured) falls
# straight through to whatever a developer's own .env has set, since removing
# the real env var exposes the next-highest-priority source. Disabling
# env_file here for the whole test session is the only way to make the suite
# genuinely independent of local .env content rather than independent only
# for the fields TEST_ENV happens to pin.
from transcribe_api.runtime import settings_dictation, settings_recording  # noqa: E402

settings_recording.Settings.model_config["env_file"] = None
settings_dictation.LocalSettings.model_config["env_file"] = None


def _clear_settings_caches() -> None:
    from transcribe_api.runtime import settings_dictation, settings_recording

    for module in (settings_dictation, settings_recording):
        getter = getattr(module, "get_settings", None)
        cache_clear = getattr(getter, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch: pytest.MonkeyPatch):
    """Pin a known environment and stop settings leaking between suites."""
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    _clear_settings_caches()
    yield
    _clear_settings_caches()


class FakeSpeechCredential:
    """Stands in for DefaultAzureCredential in stt/speech_auth.py."""

    instances = 0

    def __init__(self, *args, **kwargs) -> None:
        type(self).instances += 1
        self.scopes: list[tuple[str, ...]] = []
        self.error: Exception | None = None

    def get_token(self, *scopes, **kwargs):
        from azure.core.credentials import AccessToken

        self.scopes.append(scopes)
        if self.error is not None:
            raise self.error
        return AccessToken("aad-token", 9999999999)


@pytest.fixture
def speech_workload_identity(monkeypatch: pytest.MonkeyPatch):
    """No Speech key, as in AKS: Speech calls authenticate with a fake Entra credential.

    Returns the credential the helper uses; set ``.error`` to make get_token fail.
    """
    from transcribe_api.stt import speech_auth

    monkeypatch.setenv("AZURE_SPEECH_KEY", "")
    _clear_settings_caches()
    FakeSpeechCredential.instances = 0
    monkeypatch.setattr(speech_auth, "DefaultAzureCredential", FakeSpeechCredential)
    speech_auth._credential.cache_clear()
    credential = speech_auth._credential()
    yield credential
    speech_auth._credential.cache_clear()


def pytest_sessionfinish(session, exitstatus):
    """DIAAT-287: write the session's prohibited-content scan evidence (both areas), if
    any validation scenario ran. TELEMETRY_PII_SCAN_REPORT overrides the path."""
    import os
    import platform
    import subprocess
    from datetime import UTC, datetime
    from importlib.metadata import version
    from pathlib import Path

    from tests.unit import telemetry_scan

    if not telemetry_scan.SCENARIOS:
        return
    try:
        commit = os.environ.get("GIT_COMMIT") or subprocess.run(  # noqa: S603 - fixed arguments
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - evidence metadata is best effort
        commit = None
    path = Path(os.environ.get("TELEMETRY_PII_SCAN_REPORT", Path("test-results") / "telemetry-pii-scan.json"))
    telemetry_scan.write_evidence(
        path,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        commit=commit,
        python=platform.python_version(),
        hmcts_otel=version("hmcts-otel"),
    )
