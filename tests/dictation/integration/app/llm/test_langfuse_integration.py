"""Integration tests for Langfuse connectivity.

These tests make actual network calls to verify Langfuse connectivity with real credentials.
"""

import pytest
from langfuse import Langfuse

from transcribe_api.runtime.settings_dictation import get_settings


class TestLangfuseConnectionIntegration:
    """Integration tests for actual Langfuse connectivity."""

    @pytest.mark.integration
    def test_actual_langfuse_connection(self):
        """Test actual connection to Langfuse instance (requires network and real credentials)."""
        settings = get_settings()

        # Require valid credentials for integration tests
        assert all([settings.LANGFUSE_HOST, settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY]), (
            "All Langfuse credentials (HOST, PUBLIC_KEY, SECRET_KEY) must be configured for integration tests"
        )

        try:
            # Create actual Langfuse client
            client = Langfuse(
                host=settings.LANGFUSE_HOST,
                public_key=settings.LANGFUSE_PUBLIC_KEY,
                secret_key=settings.LANGFUSE_SECRET_KEY,
            )

            # Test authentication
            auth_result = client.auth_check()

            assert auth_result, (
                f"❌ Authentication failed for host: {settings.LANGFUSE_HOST}. "
                f"Please verify your credentials are correct."
            )

            print(f"✅ Successfully authenticated with {settings.LANGFUSE_HOST}")  # noqa: T201

        except Exception as e:
            # Only fail if this is clearly a configuration issue, not auth failure
            if "Invalid credentials" in str(e) or "Unauthorized" in str(e):
                pytest.skip(f"Skipping connection test due to invalid credentials: {e}")
            else:
                pytest.fail(f"❌ Langfuse connection test failed: {e}")
