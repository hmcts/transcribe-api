"""Unit tests for TranscriptionPollingService._should_skip_blob."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def service():
    with (
        patch("transcribe_api.stt.work_poller.get_settings"),
        patch("transcribe_api.stt.work_poller.AsyncAzureBlobManager"),
    ):
        from transcribe_api.stt.work_poller import TranscriptionPollingService

        return TranscriptionPollingService()


def make_blob(name: str, metadata: dict | None = None) -> dict:
    return {"name": name, "metadata": metadata or {}}


class TestShouldSkipBlob:
    def test_new_mp3_is_processed(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.mp3")
        assert service._should_skip_blob(blob) is False

    def test_new_mp4_is_processed(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.mp4")
        assert service._should_skip_blob(blob) is False

    def test_new_webm_is_processed(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.webm")
        assert service._should_skip_blob(blob) is False

    def test_new_wav_is_processed(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.wav")
        assert service._should_skip_blob(blob) is False

    def test_new_m4a_is_processed(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.m4a")
        assert service._should_skip_blob(blob) is False

    def test_new_ogg_is_processed(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.ogg")
        assert service._should_skip_blob(blob) is False

    def test_new_aac_is_processed(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.aac")
        assert service._should_skip_blob(blob) is False

    def test_new_mov_is_processed(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.mov")
        assert service._should_skip_blob(blob) is False

    def test_unsupported_extension_is_skipped(self, service):
        blob = make_blob("user-uploads/user@example.com/document.txt")
        assert service._should_skip_blob(blob) is True

    def test_extension_check_is_case_insensitive(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.MP3")
        assert service._should_skip_blob(blob) is False

    def test_wrong_prefix_is_skipped(self, service):
        blob = make_blob("other-container/user@example.com/audio.mp3")
        assert service._should_skip_blob(blob) is True

    def test_already_processed_is_skipped(self, service):
        blob = make_blob(
            "user-uploads/user@example.com/audio.mp3",
            metadata={"processed": "true"},
        )
        assert service._should_skip_blob(blob) is True

    def test_in_progress_is_skipped(self, service):
        blob = make_blob(
            "user-uploads/user@example.com/audio.mp3",
            metadata={"status": "in_progress"},
        )
        assert service._should_skip_blob(blob) is True

    def test_permanently_failed_is_skipped(self, service):
        blob = make_blob(
            "user-uploads/user@example.com/audio.mp3",
            metadata={"status": "permanently_failed"},
        )
        assert service._should_skip_blob(blob) is True

    def test_retrying_is_not_skipped(self, service):
        """Blobs marked retrying should be reprocessed."""
        blob = make_blob(
            "user-uploads/user@example.com/audio.mp3",
            metadata={"status": "retrying"},
        )
        assert service._should_skip_blob(blob) is False

    def test_reset_from_stale_is_not_skipped(self, service):
        """Blobs reset from stale should be reprocessed."""
        blob = make_blob(
            "user-uploads/user@example.com/audio.mp3",
            metadata={"status": "reset_from_stale"},
        )
        assert service._should_skip_blob(blob) is False

    def test_no_metadata_is_not_skipped(self, service):
        """New blobs with no metadata should be processed."""
        blob = make_blob("user-uploads/user@example.com/audio.mp3")
        assert service._should_skip_blob(blob) is False


    def test_not_user_uploads_prefix_skipped(self, service):
        blob = make_blob("other-container/user@example.com/audio.mp3")
        assert service._should_skip_blob(blob) is True

    def test_unsupported_extension_skipped(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.txt")
        assert service._should_skip_blob(blob) is True

    def test_already_processed_blob_skipped(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.mp3", {"processed": "true"})
        assert service._should_skip_blob(blob) is True

    def test_permanently_failed_blob_skipped(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.mp3", {"status": "permanently_failed"})
        assert service._should_skip_blob(blob) is True

    def test_in_progress_blob_skipped(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.mp3", {"status": "in_progress"})
        assert service._should_skip_blob(blob) is True

    def test_retrying_blob_not_skipped(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.mp3", {"status": "retrying"})
        assert service._should_skip_blob(blob) is False

    def test_reset_from_stale_not_skipped(self, service):
        blob = make_blob("user-uploads/user@example.com/audio.mp3", {"status": "reset_from_stale"})
        assert service._should_skip_blob(blob) is False


class TestExtractUserEmailFromBlobPath:
    def test_extracts_email_from_valid_path(self, service):
        result = service.extract_user_email_from_blob_path("user-uploads/user@example.com/audio.mp3")
        assert result == "user@example.com"

    def test_returns_none_for_invalid_path(self, service):
        result = service.extract_user_email_from_blob_path("other-prefix/audio.mp3")
        assert result is None

    def test_returns_none_for_short_path(self, service):
        result = service.extract_user_email_from_blob_path("user-uploads")
        assert result is None

    def test_handles_nested_path(self, service):
        result = service.extract_user_email_from_blob_path("user-uploads/user@test.com/folder/file.wav")
        assert result == "user@test.com"


class TestGetOrCreateUserByEmail:
    def test_returns_user_when_found(self, service):
        from unittest.mock import patch

        from transcribe_api.domain.models_dictation import User
        mock_user = MagicMock(spec=User)
        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = mock_user
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        with patch("transcribe_api.stt.work_poller.Session", return_value=mock_ctx):
            result = service.get_or_create_user_by_email("user@example.com")
        assert result is mock_user

    def test_returns_none_when_not_found(self, service):
        from unittest.mock import patch
        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = None
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        with patch("transcribe_api.stt.work_poller.Session", return_value=mock_ctx):
            result = service.get_or_create_user_by_email("unknown@example.com")
        assert result is None

    def test_returns_none_on_exception(self, service):
        from unittest.mock import patch
        with patch("transcribe_api.stt.work_poller.Session", side_effect=RuntimeError("db error")):
            result = service.get_or_create_user_by_email("user@example.com")
        assert result is None


@pytest.mark.asyncio
class TestPollForNewAudioFiles:
    async def test_returns_unprocessed_blobs(self, service):
        from unittest.mock import AsyncMock
        blobs = [
            {"name": "user-uploads/user@example.com/audio.mp3", "metadata": {}, "last_modified": None, "size": 100},
        ]
        service.azure_blob_manager.list_blobs_in_prefix = AsyncMock(return_value=blobs)
        result = await service.poll_for_new_audio_files()
        assert len(result) == 1

    async def test_skips_processed_blobs(self, service):
        from unittest.mock import AsyncMock
        blobs = [
            {"name": "user-uploads/user@example.com/audio.mp3", "metadata": {"processed": "true"}, "last_modified": None, "size": 100},
        ]
        service.azure_blob_manager.list_blobs_in_prefix = AsyncMock(return_value=blobs)
        result = await service.poll_for_new_audio_files()
        assert result == []

    async def test_marks_permanently_failed_when_max_retries_exceeded(self, service):
        from unittest.mock import AsyncMock
        blobs = [
            {"name": "user-uploads/user@example.com/audio.mp3", "metadata": {"retry_count": "2"}, "last_modified": None, "size": 100},
        ]
        service.azure_blob_manager.list_blobs_in_prefix = AsyncMock(return_value=blobs)
        service.azure_blob_manager._mark_blob_permanently_failed = AsyncMock()
        service._mark_blob_permanently_failed = AsyncMock()
        result = await service.poll_for_new_audio_files()
        assert result == []

    async def test_returns_empty_on_exception(self, service):
        from unittest.mock import AsyncMock
        service.azure_blob_manager.list_blobs_in_prefix = AsyncMock(side_effect=RuntimeError("network error"))
        result = await service.poll_for_new_audio_files()
        assert result == []


@pytest.mark.asyncio
class TestMarkBlobInProgress:
    async def test_returns_true_on_success(self, service):
        from unittest.mock import AsyncMock
        service.azure_blob_manager.get_blob_metadata = AsyncMock(return_value={})
        service.azure_blob_manager.set_blob_metadata = AsyncMock(return_value=True)
        result = await service._mark_blob_in_progress("user-uploads/user/audio.mp3")
        assert result is True

    async def test_returns_false_when_already_in_progress(self, service):
        from unittest.mock import AsyncMock
        service.azure_blob_manager.get_blob_metadata = AsyncMock(return_value={"status": "in_progress"})
        result = await service._mark_blob_in_progress("user-uploads/user/audio.mp3")
        assert result is False

    async def test_returns_false_when_set_metadata_fails(self, service):
        from unittest.mock import AsyncMock
        service.azure_blob_manager.get_blob_metadata = AsyncMock(return_value={})
        service.azure_blob_manager.set_blob_metadata = AsyncMock(return_value=False)
        result = await service._mark_blob_in_progress("user-uploads/user/audio.mp3")
        assert result is False

    async def test_returns_false_on_exception(self, service):
        from unittest.mock import AsyncMock
        service.azure_blob_manager.get_blob_metadata = AsyncMock(side_effect=RuntimeError("network"))
        result = await service._mark_blob_in_progress("user-uploads/user/audio.mp3")
        assert result is False
