"""Unit tests for TranscriptionPollingService._should_skip_blob."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

# Imported at module level, not under the get_settings patch below: importing
# work_poller pulls in the pipeline, which builds clients from settings at import.
from transcribe_api.stt.work_poller import STALE_CLAIM_AFTER, TranscriptionPollingService


@pytest.fixture
def service():
    with (
        patch("transcribe_api.stt.work_poller.get_settings"),
        patch("transcribe_api.stt.work_poller.AsyncAzureBlobManager"),
    ):
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

    def test_fresh_claim_is_skipped(self, service):
        started = datetime.now(UTC) - timedelta(minutes=10)
        blob = make_blob(
            "user-uploads/user@example.com/audio.mp3",
            metadata={"status": "in_progress", "started_at": started.isoformat()},
        )
        assert service._should_skip_blob(blob) is True

    def test_stale_claim_is_not_skipped(self, service):
        started = datetime.now(UTC) - STALE_CLAIM_AFTER - timedelta(minutes=1)
        blob = make_blob(
            "user-uploads/user@example.com/audio.mp3",
            metadata={"status": "in_progress", "started_at": started.isoformat()},
        )
        assert service._should_skip_blob(blob) is False

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
            {
                "name": "user-uploads/user@example.com/audio.mp3",
                "metadata": {"processed": "true"},
                "last_modified": None,
                "size": 100,
            },
        ]
        service.azure_blob_manager.list_blobs_in_prefix = AsyncMock(return_value=blobs)
        result = await service.poll_for_new_audio_files()
        assert result == []

    async def test_marks_permanently_failed_when_max_retries_exceeded(self, service):
        from unittest.mock import AsyncMock

        blobs = [
            {
                "name": "user-uploads/user@example.com/audio.mp3",
                "metadata": {"retry_count": "2"},
                "last_modified": None,
                "size": 100,
            },
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


def blob_properties(metadata: dict, etag: str = '"0x1"') -> dict:
    return {"name": "user-uploads/user/audio.mp3", "metadata": metadata, "etag": etag, "last_modified": None}


@pytest.mark.asyncio
class TestMarkBlobInProgress:
    async def test_returns_true_on_success(self, service):
        from unittest.mock import AsyncMock
        service.azure_blob_manager.get_blob_properties = AsyncMock(return_value=blob_properties({}))
        service.azure_blob_manager.set_blob_metadata = AsyncMock(return_value=True)
        result = await service._mark_blob_in_progress("user-uploads/user/audio.mp3")
        assert result is True
        assert service.azure_blob_manager.set_blob_metadata.await_args.kwargs["etag"] == '"0x1"'

    async def test_returns_false_when_already_in_progress(self, service):
        from unittest.mock import AsyncMock
        service.azure_blob_manager.get_blob_properties = AsyncMock(
            return_value=blob_properties({"status": "in_progress"})
        )
        service.azure_blob_manager.set_blob_metadata = AsyncMock(return_value=True)
        result = await service._mark_blob_in_progress("user-uploads/user/audio.mp3")
        assert result is False
        service.azure_blob_manager.set_blob_metadata.assert_not_awaited()

    async def test_returns_false_when_set_metadata_fails(self, service):
        from unittest.mock import AsyncMock
        service.azure_blob_manager.get_blob_properties = AsyncMock(return_value=blob_properties({}))
        service.azure_blob_manager.set_blob_metadata = AsyncMock(return_value=False)
        result = await service._mark_blob_in_progress("user-uploads/user/audio.mp3")
        assert result is False

    async def test_returns_false_when_blob_cannot_be_read(self, service):
        from unittest.mock import AsyncMock
        service.azure_blob_manager.get_blob_properties = AsyncMock(return_value=None)
        service.azure_blob_manager.set_blob_metadata = AsyncMock(return_value=True)
        result = await service._mark_blob_in_progress("user-uploads/user/audio.mp3")
        assert result is False
        service.azure_blob_manager.set_blob_metadata.assert_not_awaited()

    async def test_returns_false_on_exception(self, service):
        from unittest.mock import AsyncMock
        service.azure_blob_manager.get_blob_properties = AsyncMock(side_effect=RuntimeError("network"))
        result = await service._mark_blob_in_progress("user-uploads/user/audio.mp3")
        assert result is False


class TestJourney:
    """The poller attaches no journey: the file name's UUID is client input and may
    name another user's transcription. The pipeline attaches it after the save."""

    @pytest.mark.asyncio
    async def test_no_journey_before_the_transcription_is_saved(self, service):
        from unittest.mock import AsyncMock, MagicMock, patch

        from hmcts_otel import current_journey

        tid = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
        seen = {}

        async def fake_pipeline(**kwargs):
            seen["journey"] = current_journey()

        user = MagicMock(id="11111111-1111-1111-1111-111111111111", email="j@x.com", azure_user_id="oid")
        with (
            patch.object(service, "_mark_blob_in_progress", AsyncMock(return_value=True)),
            patch.object(service, "resolve_blob_owner_from_path", return_value=user),
            patch.object(service, "_mark_blob_as_processed_and_soft_delete", AsyncMock()),
            patch("transcribe_api.stt.work_poller.extract_transcription_id_from_blob_path", return_value=tid),
            patch("transcribe_api.stt.work_poller.transcribe_and_generate_llm_output", fake_pipeline),
        ):
            assert await service.process_discovered_audio(make_blob(f"user-uploads/oid/{tid}.mp4"))
        assert seen["journey"] is None


class TestErrorLogging:
    @pytest.mark.asyncio
    async def test_a_pipeline_error_message_is_not_logged(self, service):
        """The traceback's last line is the exception's text (e.g. Azure's error message
        in an HTTPException detail): only the type and status are logged.

        The record is read by a handler on the app logger, before the hmcts-otel root
        handler's filter: that filter drops exc_info only while exception-message
        capture is off, so the call site must not pass the text at all."""
        import io
        import logging
        from unittest.mock import AsyncMock, MagicMock, patch

        from fastapi import HTTPException

        from transcribe_api.runtime.logger import logger as app_logger

        sentinel = "SENTINEL-azure-error-detail"
        tid = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
        user = MagicMock(id="11111111-1111-1111-1111-111111111111", email="j@x.com", azure_user_id="oid")
        raw = io.StringIO()
        handler = logging.StreamHandler(raw)
        handler.setFormatter(logging.Formatter("%(message)s"))
        app_logger.addHandler(handler)
        try:
            with (
                patch.object(service, "_mark_blob_in_progress", AsyncMock(return_value=True)),
                patch.object(service, "resolve_blob_owner_from_path", return_value=user),  # CNP: by OID
                patch.object(service, "_mark_blob_with_error", AsyncMock()) as mark_error,
                patch(
                    "transcribe_api.stt.work_poller.extract_transcription_id_from_blob_path",
                    return_value=tid,
                ),
                patch(
                    "transcribe_api.stt.work_poller.transcribe_and_generate_llm_output",
                    AsyncMock(side_effect=HTTPException(status_code=422, detail=sentinel)),
                ),
            ):
                assert not await service.process_discovered_audio(make_blob(f"user-uploads/oid/{tid}.mp4"))
        finally:
            app_logger.removeHandler(handler)

        assert sentinel not in raw.getvalue()
        assert "HTTPException, status=422" in raw.getvalue()
        assert sentinel not in str(mark_error.await_args)
