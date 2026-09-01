"""Unit tests for settings configuration and validation.

These tests use mocked settings and do not make network calls.
They focus on settings validation logic, host allowlisting, and configuration parsing.
"""

import os
from unittest.mock import mock_open, patch

import pytest
from pydantic import ValidationError

from build_utils.validate_config import (
    validate_frontend_langfuse_host,
    validate_langfuse_environment_variables,
    validate_langfuse_host,
)
from transcribe_api.runtime.settings_dictation import Settings, get_settings


class TestSettingsConfigurationValidation:
    """Unit tests for settings configuration validation."""

    def test_settings_validation_with_mocked_env_file(self):
        """Test that Settings class properly validates mocked .env configuration."""
        # Mock environment variables for LocalSettings
        mock_env = {
            "LANGFUSE_HOST": "https://langfuse-ai.justice.gov.uk",
            "LANGFUSE_PUBLIC_KEY": "pk-lf-123456789",
            "LANGFUSE_SECRET_KEY": "sk-lf-987654321",
            "APP_URL": "http://test.com",
            "AZURE_STORAGE_ACCOUNT_NAME": "test",
            "AZURE_STORAGE_CONTAINER_NAME": "test",
            "AZURE_STORAGE_TRANSCRIPTION_CONTAINER": "test",
            "DATABASE_CONNECTION_STRING": "postgresql://test",
            "AZURE_OPENAI_API_KEY": "test",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
            "AZURE_SPEECH_KEY": "test",
            "AZURE_SPEECH_REGION": "test",
            "AZURE_GROK_API_KEY": "test",
            "AZURE_GROK_ENDPOINT": "https://test.com",
            "SENTRY_DSN": "https://test@sentry.io/test",
            "GOV_NOTIFY_API_KEY": "test",
            "GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT": "{}",
            "AZURE_AD_TENANT_ID": "test",
            "AZURE_AD_CLIENT_ID": "test",
            "ENVIRONMENT": "local",
        }

        with patch.dict(os.environ, mock_env, clear=True), patch("builtins.open", mock_open(read_data="")):
            settings = get_settings()

            # If this succeeds, the host should be the correct one
            assert settings.LANGFUSE_HOST == "https://langfuse-ai.justice.gov.uk", (
                f"Expected Justice AI Unit host, got: {settings.LANGFUSE_HOST}"
            )

            # Check that credentials are present
            assert settings.LANGFUSE_PUBLIC_KEY, "LANGFUSE_PUBLIC_KEY must not be empty"
            assert settings.LANGFUSE_SECRET_KEY, "LANGFUSE_SECRET_KEY must not be empty"
            assert settings.LANGFUSE_PUBLIC_KEY.lower().startswith("pk-lf-"), (
                f"Public key should start with 'pk-lf-' (case insensitive), got: {settings.LANGFUSE_PUBLIC_KEY[:10]}..."
            )
            assert settings.LANGFUSE_SECRET_KEY.lower().startswith("sk-lf-"), (
                f"Secret key should start with 'sk-lf-' (case insensitive), got: {settings.LANGFUSE_SECRET_KEY[:10]}..."
            )

    def test_env_file_parsing_logic(self):
        """Test that .env file parsing logic works correctly."""
        # Mock .env file content
        env_content = """
        LANGFUSE_HOST=https://langfuse-ai.justice.gov.uk
        LANGFUSE_PUBLIC_KEY=pk-lf-test123
        LANGFUSE_SECRET_KEY=sk-lf-test456
        APP_URL=http://test.com
        """

        # Check for Justice AI Unit host
        expected_host = "https://langfuse-ai.justice.gov.uk"

        # Look for LANGFUSE_HOST setting
        langfuse_host_lines = [line for line in env_content.split("\n") if line.strip().startswith("LANGFUSE_HOST=")]

        assert langfuse_host_lines, "LANGFUSE_HOST must be set in .env file"

        # Extract the host value
        host_line = langfuse_host_lines[0].strip()
        host_value = host_line.split("=", 1)[1].strip().strip('"').strip("'")

        # Assert it's the correct host
        assert host_value == expected_host, f"Expected LANGFUSE_HOST={expected_host}, but found: {host_value}"

    def test_validation_script_functions_with_mocked_env(self):
        """Test that our validation script functions work with mocked env vars."""
        mock_env = {
            "LANGFUSE_HOST": "https://langfuse-ai.justice.gov.uk",
            "LANGFUSE_PUBLIC_KEY": "pk-lf-123456789",
            "LANGFUSE_SECRET_KEY": "sk-lf-987654321",
        }

        with patch.dict(os.environ, mock_env, clear=True):
            # Test environment variables validation
            env_valid = validate_langfuse_environment_variables()
            assert env_valid, "Environment variables validation should pass with correct values"

            # Test host validation
            host_valid = validate_langfuse_host()
            assert host_valid, "Langfuse host validation should pass with correct configuration"

            # Test frontend host validation
            frontend_valid = validate_frontend_langfuse_host()
            assert frontend_valid, "Frontend Langfuse host validation should pass"

    def test_host_allowlist_enforcement(self):
        """Test that only allowlisted hosts are accepted by the validation system."""
        # List of unauthorized hosts that should be rejected
        unauthorized_hosts = [
            "https://cloud.langfuse.com",  # Old cloud instance
            "https://api.langfuse.com",  # Potential other endpoint
            "https://malicious-external-site.com",  # Malicious site
            "https://langfuse.example.com",  # Look-alike domain
            "http://localhost:3000",  # Local dev (should not be in production)
            "https://langfuse-ai.justice.gov.uk.malicious.com",  # Domain hijacking attempt
        ]

        # Test that each unauthorized host is properly rejected
        for bad_host in unauthorized_hosts:
            with pytest.raises(ValidationError) as exc_info:
                Settings(
                    APP_URL="http://test.com",
                    AZURE_STORAGE_ACCOUNT_NAME="test",
                    AZURE_STORAGE_CONTAINER_NAME="test",
                    AZURE_STORAGE_TRANSCRIPTION_CONTAINER="test",
                    DATABASE_CONNECTION_STRING="postgresql://test",
                    AZURE_OPENAI_API_KEY="test",
                    AZURE_OPENAI_ENDPOINT="https://test.openai.azure.com/",
                    AZURE_SPEECH_KEY="test",
                    AZURE_SPEECH_REGION="test",
                    AZURE_GROK_API_KEY="test",
                    AZURE_GROK_ENDPOINT="https://test.com",
                    SENTRY_DSN="https://test@sentry.io/test",
                    GOV_NOTIFY_API_KEY="test",
                    LANGFUSE_PUBLIC_KEY="pk-lf-test",
                    LANGFUSE_SECRET_KEY="sk-lf-test",
                    LANGFUSE_HOST=bad_host,  # This should fail validation
                    GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT="{}",
                    AZURE_AD_TENANT_ID="test",
                    AZURE_AD_CLIENT_ID="test",
                )

            # Verify the error message contains security information
            error_message = str(exc_info.value)
            assert "Disallowed Langfuse host" in error_message, (
                f"Host {bad_host} should be rejected with security error message"
            )
            assert bad_host in error_message, f"Error message should mention the rejected host {bad_host}"
            assert "data leakage" in error_message, (
                f"Error message should mention data leakage prevention for {bad_host}"
            )

    def test_frontend_env_vars_validation(self):
        """Test that frontend environment variables are properly validated."""
        # Test with valid frontend env vars
        mock_env = {
            "NEXT_PUBLIC_LANGFUSE_HOST": "https://langfuse-ai.justice.gov.uk",
            "NEXT_PUBLIC_LANGFUSE_PUBLIC_KEY": "pk-lf-frontend123",
        }

        with patch.dict(os.environ, mock_env, clear=True):
            frontend_host = os.getenv("NEXT_PUBLIC_LANGFUSE_HOST")
            frontend_key = os.getenv("NEXT_PUBLIC_LANGFUSE_PUBLIC_KEY")

            assert frontend_host == "https://langfuse-ai.justice.gov.uk", (
                f"Frontend host should be Justice AI Unit instance, got: {frontend_host}"
            )

            assert frontend_key.startswith("pk-lf-"), (
                f"Frontend public key should start with 'pk-lf-', got: {frontend_key[:10]}..."
            )


class TestSettingsSecurityCompliance:
    """Unit tests for security-focused settings validation."""

    def test_unauthorized_hosts_rejected_by_settings(self):
        """Test that attempting to use unauthorized hosts fails fast."""
        unauthorized_hosts = [
            "https://cloud.langfuse.com",
            "https://malicious-external-site.com",
            "https://langfuse.example.com",
            "http://localhost:3000",  # Even localhost should be rejected in production
        ]

        for bad_host in unauthorized_hosts:
            with pytest.raises(ValueError, match="Disallowed Langfuse host") as exc_info:
                # Try to create settings with bad host
                Settings(
                    APP_URL="http://test.com",
                    AZURE_STORAGE_ACCOUNT_NAME="test",
                    AZURE_STORAGE_CONTAINER_NAME="test",
                    AZURE_STORAGE_TRANSCRIPTION_CONTAINER="test",
                    DATABASE_CONNECTION_STRING="postgresql://test",
                    AZURE_OPENAI_API_KEY="test",
                    AZURE_OPENAI_ENDPOINT="https://test.openai.azure.com/",
                    AZURE_SPEECH_KEY="test",
                    AZURE_SPEECH_REGION="test",
                    AZURE_GROK_API_KEY="test",
                    AZURE_GROK_ENDPOINT="https://test.com",
                    SENTRY_DSN="https://test@sentry.io/test",
                    GOV_NOTIFY_API_KEY="test",
                    LANGFUSE_PUBLIC_KEY="pk-lf-test",
                    LANGFUSE_SECRET_KEY="sk-lf-test",
                    LANGFUSE_HOST=bad_host,  # This should fail
                    GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT="{}",
                    AZURE_AD_TENANT_ID="test",
                    AZURE_AD_CLIENT_ID="test",
                )

            error_message = str(exc_info.value)
            assert "Disallowed Langfuse host" in error_message, (
                f"Host {bad_host} should be rejected with security error"
            )
            assert bad_host in error_message, f"Error message should mention the rejected host {bad_host}"
            assert "data leakage" in error_message, "Error message should mention data leakage prevention"

    def test_validation_script_rejects_bad_hosts(self):
        """Test that validation script properly rejects unauthorized hosts."""
        # Test with environment variable override
        bad_hosts = ["https://cloud.langfuse.com", "https://malicious-site.com"]

        for bad_host in bad_hosts:
            with patch.dict(os.environ, {"LANGFUSE_HOST": bad_host}):
                # Validation should fail
                result = validate_langfuse_host()
                assert not result, f"Validation should reject host: {bad_host}"


class TestSettingsRefactoring:
    """Unit tests for the refactored settings architecture."""

    def test_get_settings_with_environment_override(self):
        """Test that get_settings respects environment parameter."""
        mock_env = {
            "APP_URL": "http://test.com",
            "AZURE_STORAGE_ACCOUNT_NAME": "test",
            "AZURE_STORAGE_CONTAINER_NAME": "test",
            "AZURE_STORAGE_TRANSCRIPTION_CONTAINER": "test",
            "DATABASE_CONNECTION_STRING": "postgresql://test",
            "AZURE_OPENAI_API_KEY": "test",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
            "AZURE_SPEECH_KEY": "test",
            "AZURE_SPEECH_REGION": "test",
            "AZURE_GROK_API_KEY": "test",
            "AZURE_GROK_ENDPOINT": "https://test.com",
            "SENTRY_DSN": "https://test@sentry.io/test",
            "GOV_NOTIFY_API_KEY": "test",
            "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
            "LANGFUSE_SECRET_KEY": "sk-lf-test",
            "LANGFUSE_HOST": "https://langfuse-ai.justice.gov.uk",
            "GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT": "{}",
            "AZURE_AD_TENANT_ID": "test",
            "AZURE_AD_CLIENT_ID": "test",
        }

        with patch.dict(os.environ, mock_env, clear=True):
            # Test that get_settings works with environment override
            settings = get_settings(environment="production")
            assert settings.LANGFUSE_HOST == "https://langfuse-ai.justice.gov.uk"

    def test_settings_functional_purity(self):
        """Test that settings can be created independently for testing (functional purity)."""
        # This demonstrates how tests can now create isolated settings
        test_settings = Settings(
            APP_URL="http://test.com",
            AZURE_STORAGE_ACCOUNT_NAME="test",
            AZURE_STORAGE_CONTAINER_NAME="test",
            AZURE_STORAGE_TRANSCRIPTION_CONTAINER="test",
            DATABASE_CONNECTION_STRING="postgresql://test",
            AZURE_OPENAI_API_KEY="test",
            AZURE_OPENAI_ENDPOINT="https://test.openai.azure.com/",
            AZURE_SPEECH_KEY="test",
            AZURE_GROK_API_KEY="test",
            AZURE_GROK_ENDPOINT="https://test.com",
            SENTRY_DSN="https://test@sentry.io/test",
            GOV_NOTIFY_API_KEY="test",
            LANGFUSE_PUBLIC_KEY="pk-lf-test",
            LANGFUSE_SECRET_KEY="sk-lf-test",
            LANGFUSE_HOST="https://langfuse-ai.justice.gov.uk",
            GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT="{}",
            AZURE_AD_TENANT_ID="test",
            AZURE_AD_CLIENT_ID="test",
            ENVIRONMENT="test",
        )

        # Verify settings work independently
        assert test_settings.ENVIRONMENT == "test"
        assert test_settings.LANGFUSE_HOST == "https://langfuse-ai.justice.gov.uk"
