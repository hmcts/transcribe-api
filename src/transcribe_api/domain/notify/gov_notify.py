from notifications_python_client.notifications import NotificationsAPIClient

from transcribe_api.runtime.settings_dictation import get_settings


def _get_notifications_client() -> NotificationsAPIClient | None:
    """Create the GOV.UK Notify client lazily.

    Returns None when GOV_NOTIFY_API_KEY is not set, keeping module imports
    safe in tests that set placeholder API keys but never exercise the
    email-sending path.
    """
    api_key = get_settings().GOV_NOTIFY_API_KEY
    if not api_key:
        return None
    return NotificationsAPIClient(api_key)


def send_email(user_email: str, meeting_link: str, meeting_title: str):
    client = _get_notifications_client()
    if client is None:
        return
    client.send_email_notification(
        email_address=user_email,
        template_id="1ae60bea-3fe2-4f27-ac35-441256ab7a1a",
        personalisation={
            "meeting_link": meeting_link,
            "meeting_title": meeting_title,
        },
    )
