import os

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_CONNECTION_STRING", "postgresql://test:test@localhost:5432/test_db")
os.environ.setdefault("AZURE_SPEECH_KEY", "test-key")
os.environ.setdefault("AZURE_SPEECH_ENDPOINT", "https://test.cognitiveservices.azure.com")
os.environ.setdefault("AZURE_STORAGE_ACCOUNT_NAME", "teststorage")
os.environ.setdefault("AZURE_STORAGE_CONTAINER_NAME", "test-container")
os.environ.setdefault("LOCAL_API_KEY", "test-api-key")


@pytest.fixture(autouse=True)
def reset_settings_cache():
    from transcribe_api.runtime.settings_recording import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
