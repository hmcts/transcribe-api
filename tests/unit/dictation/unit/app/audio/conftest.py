"""Shared fixtures for app/audio unit tests."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="module", autouse=True)
def mock_settings_for_audio():
    """Mock settings at module level to prevent import-time side effects."""
    mock_settings_obj = MagicMock()
    mock_settings_obj.DATABASE_CONNECTION_STRING = "postgresql://test:test@localhost/test"
    mock_settings_obj.ENVIRONMENT = "test"
    mock_settings_obj.AZURE_AD_TENANT_ID = "test-tenant"
    mock_settings_obj.AZURE_AD_CLIENT_ID = "test-client"
    mock_settings_obj.JWT_ENABLE_VERIFICATION = False
    mock_settings_obj.JWT_VERIFICATION_STRICT = False

    with (
        patch("transcribe_api.runtime.settings_dictation.get_settings", return_value=mock_settings_obj),
        patch("transcribe_api.runtime.db_dictation.get_settings", return_value=mock_settings_obj),
    ):
        yield mock_settings_obj
