"""Unit tests for app.database.postgres_database helpers."""

from unittest.mock import MagicMock, patch


class TestGetSession:
    def test_yields_session(self):
        from transcribe_api.runtime.db_dictation import get_session

        mock_session = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.runtime.db_dictation.Session", return_value=mock_ctx):
            gen = get_session()
            result = next(gen)
            assert result is mock_session
            gen.close()


class TestTestDbConnection:
    def test_returns_true_when_execute_succeeds(self):
        from transcribe_api.runtime.db_dictation import test_db_connection

        mock_session = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.runtime.db_dictation.Session", return_value=mock_ctx):
            result = test_db_connection()

        assert result is True
        mock_session.execute.assert_called_once()

    def test_returns_false_when_execute_raises(self):
        from transcribe_api.runtime.db_dictation import test_db_connection

        mock_session = MagicMock()
        mock_session.execute.side_effect = Exception("connection refused")
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.runtime.db_dictation.Session", return_value=mock_ctx):
            result = test_db_connection()

        assert result is False
