"""Unit tests for process_audio_fully orchestration functions."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

# Provide minimal env vars so postgres_database can import without a real DB.
# These are only set if not already present; a real .env file takes precedence.
for _var, _val in [
    ("DATABASE_CONNECTION_STRING", "postgresql://test:test@localhost/test"),
    ("APP_URL", "http://localhost:8000"),
    ("AZURE_AD_CLIENT_ID", "test-client"),
    ("AZURE_AD_TENANT_ID", "test-tenant"),
    ("AZURE_STORAGE_ACCOUNT_NAME", "test-storage"),
    ("AZURE_STORAGE_CONTAINER_NAME", "test-container"),
    ("AZURE_STORAGE_TRANSCRIPTION_CONTAINER", "test-transcription"),
]:
    os.environ.setdefault(_var, _val)

# Block LLM calls - they have their own deep import chain into Langfuse/OpenAI.
sys.modules.setdefault("transcribe_api.documents.minutes.llm_calls", MagicMock())

import pytest  # noqa: E402

from transcribe_api.stt.pipeline import (  # noqa: E402
    generate_and_save_meeting_title,
    transcribe_and_generate_llm_output,
)

# Clear the get_settings cache so other test files can create their own Settings
# instances without getting our dummy-values cached result.
from transcribe_api.runtime.settings_dictation import get_settings as _gs  # noqa: E402

_gs.cache_clear()


@pytest.fixture(autouse=True)
def _patch_external(monkeypatch):
    """Patch all external dependencies for every test in this module."""
    monkeypatch.setattr("transcribe_api.stt.pipeline.sentry_sdk", MagicMock())
    monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription", MagicMock())
    monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_job", MagicMock())
    monkeypatch.setattr("transcribe_api.stt.pipeline.generate_meeting_title", AsyncMock(return_value="Test Title"))
    monkeypatch.setattr("transcribe_api.stt.pipeline.generate_llm_output_task", AsyncMock())
    monkeypatch.setattr("transcribe_api.stt.pipeline.send_email", MagicMock())
    monkeypatch.setattr(
        "transcribe_api.stt.pipeline.transcribe_audio",
        AsyncMock(return_value=[MagicMock()]),
    )
    monkeypatch.setattr(
        "transcribe_api.stt.pipeline.process_speakers_and_dialogue_entries",
        AsyncMock(return_value=[MagicMock()]),
    )
    monkeypatch.setattr(
        "transcribe_api.stt.pipeline.get_url_for_transcription",
        MagicMock(return_value="https://app.example.com/?id=123"),
    )
    monkeypatch.setattr(
        "transcribe_api.stt.pipeline._generate_and_upload_transcript_docx",
        AsyncMock(return_value="user-uploads/user@example.com/transcript.docx"),
    )


@pytest.mark.asyncio
class TestGenerateAndSaveMeetingTitle:
    async def test_returns_updated_transcription_with_title(self, monkeypatch):
        from transcribe_api.domain.models_dictation import Transcription
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.generate_meeting_title",
            AsyncMock(return_value="Generated Title"),
        )
        mock_save = MagicMock()
        updated = MagicMock()
        updated.title = "Generated Title"
        mock_save.return_value = updated
        monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription", mock_save)

        t = Transcription(id=uuid4())
        result = await generate_and_save_meeting_title([], t, uuid4(), "user@example.com")
        assert result.title == "Generated Title"

    async def test_returns_original_transcription_on_error(self, monkeypatch):
        from transcribe_api.domain.models_dictation import Transcription
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.generate_meeting_title",
            AsyncMock(side_effect=RuntimeError("LLM failed")),
        )

        t = Transcription(id=uuid4())
        result = await generate_and_save_meeting_title([], t, uuid4(), "user@example.com")
        assert result is t


@pytest.mark.asyncio
class TestTranscribeAndGenerateLlmOutput:
    async def test_happy_path_runs_without_error(self):
        await transcribe_and_generate_llm_output(
            user_upload_blob_storage_file_key="user-uploads/user@example.com/file.wav",
            user_id=uuid4(),
            user_email="user@example.com",
            azure_user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            transcription_id=str(uuid4()),
        )

    async def test_saves_transcription_job_on_transcription_error(self, monkeypatch):

        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.transcribe_audio",
            AsyncMock(side_effect=RuntimeError("Azure error")),
        )
        mock_save_job = MagicMock()
        monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_job", mock_save_job)

        with pytest.raises(RuntimeError, match="Azure error"):
            await transcribe_and_generate_llm_output(
                user_upload_blob_storage_file_key="blob",
                user_id=uuid4(),
                user_email="user@example.com",
                azure_user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            )

        # save_transcription_job called with error_message
        mock_save_job.assert_called_once()
        job_arg = mock_save_job.call_args[0][0]
        assert job_arg.error_message == "Azure error"

    async def test_sends_email_after_completion(self, monkeypatch):
        mock_email = MagicMock()
        monkeypatch.setattr("transcribe_api.stt.pipeline.send_email", mock_email)

        await transcribe_and_generate_llm_output(
            user_upload_blob_storage_file_key="blob",
            user_id=uuid4(),
            user_email="user@example.com",
            azure_user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        )

        mock_email.assert_called_once()

    async def test_email_errors_do_not_propagate(self, monkeypatch):
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.send_email",
            MagicMock(side_effect=RuntimeError("email failed")),
        )
        # Should complete without raising
        await transcribe_and_generate_llm_output(
            user_upload_blob_storage_file_key="blob",
            user_id=uuid4(),
            user_email="user@example.com",
            azure_user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        )

    async def test_parallel_task_errors_do_not_propagate(self, monkeypatch):
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.generate_llm_output_task",
            AsyncMock(side_effect=RuntimeError("LLM task failed")),
        )
        # Should complete without raising (errors logged but swallowed)
        await transcribe_and_generate_llm_output(
            user_upload_blob_storage_file_key="blob",
            user_id=uuid4(),
            user_email="user@example.com",
            azure_user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        )
