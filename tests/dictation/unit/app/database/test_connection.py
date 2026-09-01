"""Unit tests for database connection URL helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import transcribe_api.runtime.db_connection as conn_module
from transcribe_api.runtime.db_connection import _requires_ssl, _to_asyncpg_url


class TestRequiresSsl:
    def test_azure_flexible_server_requires_ssl(self):
        url = "postgresql://user:pass@mydb.postgres.database.azure.com/db"
        assert _requires_ssl(url) is True

    def test_localhost_does_not_require_ssl(self):
        url = "postgresql://user:pass@localhost/db"
        assert _requires_ssl(url) is False

    def test_sslmode_require_returns_true(self):
        url = "postgresql://user:pass@host/db?sslmode=require"
        assert _requires_ssl(url) is True

    def test_sslmode_verify_full_returns_true(self):
        url = "postgresql://user:pass@host/db?sslmode=verify-full"
        assert _requires_ssl(url) is True

    def test_sslmode_disable_returns_false(self):
        url = "postgresql://user:pass@host/db?sslmode=disable"
        assert _requires_ssl(url) is False

    def test_no_sslmode_non_azure_returns_false(self):
        url = "postgresql://user:pass@somehost.example.com/db"
        assert _requires_ssl(url) is False


class TestToAsyncpgUrl:
    def test_converts_postgresql_prefix(self):
        url = "postgresql://user:pass@localhost/db"
        result = _to_asyncpg_url(url, require_ssl=False)
        assert result.startswith("postgresql+asyncpg://")

    def test_does_not_double_convert(self):
        url = "postgresql+asyncpg://user:pass@localhost/db"
        result = _to_asyncpg_url(url, require_ssl=False)
        assert result.count("asyncpg") == 1

    def test_adds_ssl_true_when_required(self):
        url = "postgresql://user:pass@host/db"
        result = _to_asyncpg_url(url, require_ssl=True)
        assert "ssl=true" in result

    def test_does_not_add_ssl_when_not_required(self):
        url = "postgresql://user:pass@host/db"
        result = _to_asyncpg_url(url, require_ssl=False)
        assert "ssl" not in result

    def test_strips_sslmode_param_when_ssl_required(self):
        url = "postgresql://user:pass@host/db?sslmode=require"
        result = _to_asyncpg_url(url, require_ssl=True)
        assert "sslmode" not in result
        assert "ssl=true" in result

    def test_strips_sslmode_param_when_ssl_not_required(self):
        url = "postgresql://user:pass@host/db?sslmode=disable"
        result = _to_asyncpg_url(url, require_ssl=False)
        assert "sslmode" not in result
        assert "ssl" not in result

    def test_preserves_other_query_params(self):
        url = "postgresql://user:pass@host/db?connect_timeout=10"
        result = _to_asyncpg_url(url, require_ssl=False)
        assert "connect_timeout=10" in result

    def test_no_trailing_question_mark(self):
        url = "postgresql://user:pass@host/db"
        result = _to_asyncpg_url(url, require_ssl=False)
        assert not result.endswith("?")
        assert not result.endswith("&")


class TestGetEngine:
    def test_creates_sync_engine(self):
        original = conn_module._sync_engine_singleton
        conn_module._sync_engine_singleton = None
        try:
            with patch("transcribe_api.runtime.db_connection.get_settings") as mock_get:
                settings = MagicMock()
                settings.DATABASE_CONNECTION_STRING = "postgresql://user:pass@localhost/db"
                mock_get.return_value = settings
                with patch("transcribe_api.runtime.db_connection.create_sync_engine") as mock_create:
                    mock_create.return_value = MagicMock()
                    from transcribe_api.runtime.db_connection import get_engine
                    result = get_engine()
                mock_create.assert_called_once_with(
                    "postgresql://user:pass@localhost/db", echo=False, pool_pre_ping=True
                )
                assert result is mock_create.return_value
        finally:
            conn_module._sync_engine_singleton = original


class TestGetAsyncEngine:
    def setup_method(self):
        """Reset singleton state between tests."""
        conn_module._async_engine_singleton = None
        conn_module._AsyncSessionLocal = None

    def test_creates_async_engine_for_non_azure_url(self):
        with patch("transcribe_api.runtime.db_connection.get_settings") as mock_get, patch("transcribe_api.runtime.db_connection.create_async_engine") as mock_engine, patch("transcribe_api.runtime.db_connection.async_sessionmaker"):
            settings = MagicMock()
            settings.DATABASE_CONNECTION_STRING = "postgresql://user:pass@localhost/db"
            mock_get.return_value = settings
            mock_engine.return_value = MagicMock()
            from transcribe_api.runtime.db_connection import get_async_engine
            get_async_engine()

        mock_engine.assert_called_once()
        call_kwargs = mock_engine.call_args[1]
        assert call_kwargs.get("pool_pre_ping") is True
        # No SSL for localhost
        assert "ssl" not in call_kwargs.get("connect_args", {})

    def test_creates_async_engine_with_ssl_for_azure(self):
        with patch("transcribe_api.runtime.db_connection.get_settings") as mock_get, patch("transcribe_api.runtime.db_connection.create_async_engine") as mock_engine, patch("transcribe_api.runtime.db_connection.async_sessionmaker"):
            settings = MagicMock()
            settings.DATABASE_CONNECTION_STRING = "postgresql://user:pass@mydb.postgres.database.azure.com/db"
            mock_get.return_value = settings
            mock_engine.return_value = MagicMock()
            from transcribe_api.runtime.db_connection import get_async_engine
            get_async_engine()

        call_kwargs = mock_engine.call_args[1]
        assert "ssl" in call_kwargs.get("connect_args", {})

    def test_returns_singleton_on_second_call(self):
        with patch("transcribe_api.runtime.db_connection.get_settings") as mock_get, patch("transcribe_api.runtime.db_connection.create_async_engine") as mock_engine, patch("transcribe_api.runtime.db_connection.async_sessionmaker"):
            settings = MagicMock()
            settings.DATABASE_CONNECTION_STRING = "postgresql://user:pass@localhost/db"
            mock_get.return_value = settings
            mock_engine.return_value = MagicMock()
            from transcribe_api.runtime.db_connection import get_async_engine
            r1 = get_async_engine()
            r2 = get_async_engine()

        # create_async_engine only called once due to singleton
        assert mock_engine.call_count == 1
        assert r1 is r2


@pytest.mark.asyncio
class TestGetAsyncSession:
    def setup_method(self):
        conn_module._async_engine_singleton = None
        conn_module._AsyncSessionLocal = None

    async def test_commits_on_success(self):
        from unittest.mock import MagicMock

        from transcribe_api.runtime.db_connection import get_async_session

        mock_session = AsyncMock()
        mock_session_maker = MagicMock()
        mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=False)

        conn_module._async_engine_singleton = MagicMock()
        conn_module._AsyncSessionLocal = mock_session_maker

        async with get_async_session() as session:
            assert session is mock_session

        mock_session.commit.assert_called_once()

    async def test_rolls_back_on_exception(self):
        from unittest.mock import MagicMock

        from transcribe_api.runtime.db_connection import get_async_session

        mock_session = AsyncMock()
        mock_session_maker = MagicMock()
        mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=False)

        conn_module._async_engine_singleton = MagicMock()
        conn_module._AsyncSessionLocal = mock_session_maker

        err_msg = "test error"
        with pytest.raises(RuntimeError, match=err_msg):
            async with get_async_session():
                raise RuntimeError(err_msg)

        mock_session.rollback.assert_called_once()
