"""Root conftest for the merged test suite.

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
