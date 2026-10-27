"""DIAAT-298: the less common log paths do not log the user's email.

Legacy upload paths are user-uploads/{email}/{filename}, so every log line that names
a blob must go through _redact_blob_path; user records are logged by id, not email.
"""

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.core.exceptions import ResourceNotFoundError

from transcribe_api.domain.models_dictation import User
from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager
from transcribe_api.stt.work_poller import TranscriptionPollingService

EMAIL = "judge.sentinel@example.test"
BLOB = f"user-uploads/{EMAIL}/audio.mp3"


def _assert_email_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    assert caplog.records, "expected the path under test to log"
    assert EMAIL not in caplog.text


# _resolve_user ----------------------------------------------------------------------


def _resolve_existing(user: User, roles: list[str]) -> None:
    from transcribe_api.domain.auth import dependencies_dictation as deps

    session = MagicMock()
    session.exec.return_value.first.return_value = user
    with (
        patch.object(deps, "get_engine"),
        patch.object(deps, "Session") as session_cls,
        patch.object(deps, "get_valid_roles", return_value={"Judge": "Judge", "Normal": "Normal"}),
    ):
        session_cls.return_value.__enter__.return_value = session
        deps._resolve_user("azure-123", EMAIL, roles)


def _existing_user(role: str) -> User:
    return User(id=uuid.uuid4(), email=EMAIL, azure_user_id="azure-123", role=role)


def test_a_returning_user_is_logged_by_id(caplog: pytest.LogCaptureFixture) -> None:
    user = _existing_user("Normal")
    with caplog.at_level(logging.DEBUG):
        _resolve_existing(user, ["Normal"])
    assert str(user.id) in caplog.text
    _assert_email_not_logged(caplog)


def test_a_role_sync_is_logged_by_id(caplog: pytest.LogCaptureFixture) -> None:
    user = _existing_user("Normal")
    with caplog.at_level(logging.DEBUG):
        _resolve_existing(user, ["Judge"])
    assert user.role == "Judge"
    _assert_email_not_logged(caplog)


# Upload poller ------------------------------------------------------------------------


@pytest.fixture
def poller() -> TranscriptionPollingService:
    with (
        patch("transcribe_api.stt.work_poller.get_settings"),
        patch("transcribe_api.stt.work_poller.AsyncAzureBlobManager"),
    ):
        return TranscriptionPollingService()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [False, RuntimeError("storage unavailable")])
async def test_marking_a_blob_processed_fails_without_logging_the_email(
    poller: TranscriptionPollingService, caplog: pytest.LogCaptureFixture, outcome: object
) -> None:
    poller.azure_blob_manager.set_blob_metadata = AsyncMock(
        side_effect=outcome if isinstance(outcome, Exception) else None, return_value=outcome
    )
    with caplog.at_level(logging.DEBUG):
        await poller._mark_blob_as_processed_and_soft_delete(BLOB)
    _assert_email_not_logged(caplog)


@pytest.mark.asyncio
async def test_marking_a_blob_with_an_error_fails_without_logging_the_email(
    poller: TranscriptionPollingService, caplog: pytest.LogCaptureFixture
) -> None:
    poller.azure_blob_manager.get_blob_metadata = AsyncMock(side_effect=RuntimeError("storage unavailable"))
    with caplog.at_level(logging.DEBUG):
        await poller._mark_blob_with_error(BLOB, "transcription failed")
    _assert_email_not_logged(caplog)


@pytest.mark.asyncio
async def test_marking_a_blob_permanently_failed_fails_without_logging_the_email(
    poller: TranscriptionPollingService, caplog: pytest.LogCaptureFixture
) -> None:
    poller.azure_blob_manager.set_blob_metadata = AsyncMock(side_effect=RuntimeError("storage unavailable"))
    with caplog.at_level(logging.DEBUG):
        await poller._mark_blob_permanently_failed(BLOB, {"retry_count": "3"})
    _assert_email_not_logged(caplog)


# Blob manager -------------------------------------------------------------------------


class _AsyncContext:
    def __init__(self, obj: object) -> None:
        self.obj = obj

    async def __aenter__(self) -> object:
        return self.obj

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def blob_client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def blob_manager(blob_client: MagicMock):
    service = MagicMock()
    service.get_blob_client.return_value = blob_client
    with (
        patch("transcribe_api.runtime.blob_dictation.get_settings") as settings,
        patch("transcribe_api.runtime.blob_dictation.DefaultAzureCredential"),
        patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient", return_value=_AsyncContext(service)),
    ):
        settings.return_value.AZURE_STORAGE_ACCOUNT_NAME = "account"
        settings.return_value.AZURE_STORAGE_CONTAINER_NAME = "container"
        yield AsyncAzureBlobManager()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ResourceNotFoundError("missing"), RuntimeError("storage unavailable")])
async def test_reading_blob_properties_fails_without_logging_the_email(
    blob_manager: AsyncAzureBlobManager,
    blob_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    blob_client.get_blob_properties = AsyncMock(side_effect=error)
    with caplog.at_level(logging.DEBUG):
        assert await blob_manager.get_blob_properties(BLOB) is None
    _assert_email_not_logged(caplog)


@pytest.mark.asyncio
async def test_downloading_a_missing_blob_does_not_log_the_email(
    blob_manager: AsyncAzureBlobManager, blob_client: MagicMock, caplog: pytest.LogCaptureFixture, tmp_path
) -> None:
    blob_client.exists = AsyncMock(return_value=False)
    with caplog.at_level(logging.DEBUG), pytest.raises(FileNotFoundError):
        await blob_manager.download_blob_to_file(BLOB, tmp_path / "audio.mp3")
    _assert_email_not_logged(caplog)


@pytest.mark.asyncio
async def test_a_failed_download_does_not_log_the_email(
    blob_manager: AsyncAzureBlobManager, blob_client: MagicMock, caplog: pytest.LogCaptureFixture, tmp_path
) -> None:
    blob_client.exists = AsyncMock(return_value=True)
    blob_client.download_blob = AsyncMock(side_effect=RuntimeError("storage unavailable"))
    with caplog.at_level(logging.DEBUG):
        assert await blob_manager.download_blob_to_file(BLOB, tmp_path / "audio.mp3") is False
    _assert_email_not_logged(caplog)
