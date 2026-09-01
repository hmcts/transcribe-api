"""Unit tests for gov_notify helpers."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_settings():
    with patch("transcribe_api.domain.notify.gov_notify.get_settings") as mock_get:
        settings = MagicMock()
        settings.GOV_NOTIFY_API_KEY = "test-api-key"
        mock_get.return_value = settings
        yield settings


class TestGetNotificationsClient:
    def test_returns_client_when_api_key_set(self, mock_settings):
        mock_settings.GOV_NOTIFY_API_KEY = "real-key"
        with patch("transcribe_api.domain.notify.gov_notify.NotificationsAPIClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            from transcribe_api.domain.notify.gov_notify import _get_notifications_client
            result = _get_notifications_client()
        assert result is not None
        mock_client_cls.assert_called_once_with("real-key")

    def test_returns_none_when_api_key_empty(self, mock_settings):
        mock_settings.GOV_NOTIFY_API_KEY = ""
        from transcribe_api.domain.notify.gov_notify import _get_notifications_client
        result = _get_notifications_client()
        assert result is None

    def test_returns_none_when_api_key_none(self, mock_settings):
        mock_settings.GOV_NOTIFY_API_KEY = None
        from transcribe_api.domain.notify.gov_notify import _get_notifications_client
        result = _get_notifications_client()
        assert result is None


class TestSendEmail:
    def test_sends_email_when_client_available(self, mock_settings):
        mock_client = MagicMock()
        with patch("transcribe_api.domain.notify.gov_notify.NotificationsAPIClient", return_value=mock_client):
            from transcribe_api.domain.notify.gov_notify import send_email
            send_email("user@example.com", "https://example.com/meeting", "Team Standup")

        mock_client.send_email_notification.assert_called_once_with(
            email_address="user@example.com",
            template_id="1ae60bea-3fe2-4f27-ac35-441256ab7a1a",
            personalisation={
                "meeting_link": "https://example.com/meeting",
                "meeting_title": "Team Standup",
            },
        )

    def test_does_nothing_when_no_api_key(self, mock_settings):
        mock_settings.GOV_NOTIFY_API_KEY = ""
        with patch("transcribe_api.domain.notify.gov_notify.NotificationsAPIClient") as mock_client_cls:
            from transcribe_api.domain.notify.gov_notify import send_email
            send_email("user@example.com", "https://example.com", "Title")

        mock_client_cls.assert_not_called()
