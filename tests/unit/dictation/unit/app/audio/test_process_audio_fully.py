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
    monkeypatch.setattr("transcribe_api.stt.pipeline.load_or_create_transcription", MagicMock())
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
        result = await generate_and_save_meeting_title([], t, uuid4())
        assert result.title == "Generated Title"

    async def test_returns_original_transcription_on_error(self, monkeypatch):
        from transcribe_api.domain.models_dictation import Transcription

        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.generate_meeting_title",
            AsyncMock(side_effect=RuntimeError("LLM failed")),
        )

        t = Transcription(id=uuid4())
        result = await generate_and_save_meeting_title([], t, uuid4())
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


# DIAAT-276: one journey ID gives the ordered lifecycle, outcome and failing component.


class TestJourneyTracing:
    TRANSCRIPTION_ID = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"

    @pytest.fixture(autouse=True)
    def _saved_transcription(self, monkeypatch):
        # The pipeline opens the journey from the saved row's ID (a UUID in production).
        saved = MagicMock(id=self.TRANSCRIPTION_ID, title="Title")
        monkeypatch.setattr("transcribe_api.stt.pipeline.load_or_create_transcription", MagicMock(return_value=saved))

    def _ordered(self, telemetry, journey_id):
        from hmcts_otel import conventions as c

        return [str(e[c.CUSTOM_EVENT_NAME]) for e in telemetry.events() if e.get(c.JOURNEY_ID) == journey_id]

    async def _run(self):
        from hmcts_otel import journey

        from transcribe_api.runtime.telemetry import JOURNEY_TRANSCRIPTION

        with journey(JOURNEY_TRANSCRIPTION, self.TRANSCRIPTION_ID):
            await transcribe_and_generate_llm_output(
                user_upload_blob_storage_file_key=f"user-uploads/oid/{self.TRANSCRIPTION_ID}.mp4",
                user_id=uuid4(),
                user_email="judge@justice.gov.uk",
                azure_user_id="oid",
                transcription_id=self.TRANSCRIPTION_ID,
            )

    @pytest.mark.asyncio
    async def test_successful_pipeline_lifecycle(self):
        from hmcts_otel import conventions as c
        from hmcts_otel.testing import capture_telemetry

        with capture_telemetry() as telemetry:
            await self._run()
            assert self._ordered(telemetry, self.TRANSCRIPTION_ID) == [
                "transcription.started",
                "transcription.completed",
                "speaker_naming.started",
                "speaker_naming.completed",
                "transcript.available",
                "title_generation.started",
                "title_generation.completed",
                "document_generation.started",
                "document_generation.completed",
                "notification.started",
                "notification.completed",
                "job.finished",
            ]
            assert telemetry.events("job.finished")[0][c.OUTCOME] == "success"
            # The task is its own trace (not a child of whatever started it).
            task = next(s for s in telemetry.finished_spans() if s.name == "Transcribe and Generate LLM Output")
            assert task.parent is None
            telemetry.assert_no_prohibited_content("judge@justice.gov.uk")

    @pytest.mark.asyncio
    async def test_transcription_failure_identifies_the_component(self, monkeypatch):
        from hmcts_otel import conventions as c
        from hmcts_otel.testing import capture_telemetry

        secret = "speech said " + "SECRET" + "CONTENT"
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.transcribe_audio", AsyncMock(side_effect=RuntimeError(secret))
        )
        with capture_telemetry() as telemetry:
            with pytest.raises(RuntimeError):
                await self._run()
            assert self._ordered(telemetry, self.TRANSCRIPTION_ID) == [
                "transcription.started",
                "transcription.failed",
                "job.finished",
            ]
            finished = telemetry.events("job.finished")[0]
            assert finished[c.OUTCOME] == "error"
            assert finished[c.ERROR_CODE] == "transcription.failed"
            assert telemetry.events("transcription.failed")[0][c.ERROR_CODE] == "unhandled.RuntimeError"
            telemetry.assert_no_prohibited_content(secret)

    @pytest.mark.asyncio
    async def test_email_failure_is_a_controlled_notification_failure(self, monkeypatch):
        from hmcts_otel import conventions as c
        from hmcts_otel.testing import capture_telemetry

        monkeypatch.setattr("transcribe_api.stt.pipeline.send_email", MagicMock(side_effect=RuntimeError("smtp")))
        with capture_telemetry() as telemetry:
            await self._run()
            failed = telemetry.events("notification.failed")[0]
            assert failed[c.OUTCOME] == "error"  # a server-side failure: counts as an error
            assert failed[c.ERROR_CODE] == "notification.email_failed"
            # The job completed but without its email: not reported as a success.
            finished = telemetry.events("job.finished")[0]
            assert finished[c.OUTCOME] == "error"
            assert finished[c.ERROR_CODE] == "notification.email_failed"


class TestTerminalOutcome:
    """DIAAT-276 review fixes: job.finished reflects what really happened, always once."""

    TID = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"

    async def _run(self, transcription_id=None):
        await transcribe_and_generate_llm_output(
            user_upload_blob_storage_file_key="user-uploads/oid/x.mp4",
            user_id=uuid4(),
            user_email="judge@justice.gov.uk",
            azure_user_id="oid",
            transcription_id=transcription_id,
        )

    def _saved(self, monkeypatch, tid):
        saved = MagicMock(id=tid, title="Title")
        monkeypatch.setattr("transcribe_api.stt.pipeline.load_or_create_transcription", MagicMock(return_value=saved))
        return saved

    def _finished(self, telemetry):
        from hmcts_otel import conventions as c

        events = telemetry.events("job.finished")
        assert len(events) == 1, [e[c.CUSTOM_EVENT_NAME] for e in telemetry.events()]
        return events[0][c.OUTCOME], events[0].get(c.ERROR_CODE)

    @pytest.mark.asyncio
    async def test_journey_uses_the_saved_id_for_non_uuid_file_names(self, monkeypatch):
        from hmcts_otel import conventions as c
        from hmcts_otel.testing import capture_telemetry

        self._saved(monkeypatch, self.TID)
        with capture_telemetry() as telemetry:
            await self._run(transcription_id=None)  # non-UUID upload: id generated on save
            assert {e[c.JOURNEY_ID] for e in telemetry.events()} == {self.TID}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("patch_target", "patch_value", "expected"),
        [
            ("generate_llm_output_task", AsyncMock(side_effect=RuntimeError("llm down")), ("error", "minutes.failed")),
            ("_generate_and_upload_transcript_docx", AsyncMock(return_value=None), ("error", "document.not_generated")),
            ("send_email", MagicMock(side_effect=RuntimeError("smtp")), ("error", "notification.email_failed")),
        ],
    )
    async def test_degraded_jobs_are_not_reported_as_success(self, monkeypatch, patch_target, patch_value, expected):
        from hmcts_otel.testing import capture_telemetry

        self._saved(monkeypatch, self.TID)
        monkeypatch.setattr(f"transcribe_api.stt.pipeline.{patch_target}", patch_value)
        with capture_telemetry() as telemetry:
            await self._run(self.TID)
            assert self._finished(telemetry) == expected

    @pytest.mark.asyncio
    async def test_record_save_failure_still_ends_the_journey(self, monkeypatch):
        from hmcts_otel.testing import capture_telemetry

        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.load_or_create_transcription", MagicMock(side_effect=RuntimeError("db"))
        )
        with capture_telemetry() as telemetry:
            with pytest.raises(RuntimeError):
                await self._run(self.TID)
            assert self._finished(telemetry) == ("error", "job_record.failed")

    @pytest.mark.asyncio
    async def test_failure_after_the_document_still_ends_the_journey(self, monkeypatch):
        from hmcts_otel.testing import capture_telemetry

        self._saved(monkeypatch, self.TID)
        calls = {"n": 0}

        def save_job(job, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 2:  # the update after the document is generated
                message = "db"
                raise RuntimeError(message)
            return MagicMock()

        monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_job", save_job)
        with capture_telemetry() as telemetry:
            with pytest.raises(RuntimeError):
                await self._run(self.TID)
            assert self._finished(telemetry) == ("error", "job_record.failed")

    @pytest.mark.asyncio
    async def test_speaker_naming_failure_is_attributed_to_speaker_naming(self, monkeypatch):
        from hmcts_otel.testing import capture_telemetry

        self._saved(monkeypatch, self.TID)
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.process_speakers_and_dialogue_entries",
            AsyncMock(side_effect=RuntimeError("x")),
        )
        with capture_telemetry() as telemetry:
            with pytest.raises(RuntimeError):
                await self._run(self.TID)
            assert self._finished(telemetry) == ("error", "speaker_naming.failed")

    @pytest.mark.asyncio
    async def test_a_fast_minutes_failure_waits_for_the_title(self, monkeypatch):
        import asyncio

        from hmcts_otel.testing import capture_telemetry

        saved = self._saved(monkeypatch, self.TID)
        saved.title = None

        async def slow_title(*args, **kwargs):
            await asyncio.sleep(0.05)
            return "Late Title"

        monkeypatch.setattr("transcribe_api.stt.pipeline.generate_meeting_title", slow_title)
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.generate_llm_output_task", AsyncMock(side_effect=RuntimeError("fast"))
        )
        email = MagicMock()
        monkeypatch.setattr("transcribe_api.stt.pipeline.send_email", email)
        with capture_telemetry() as telemetry:
            await self._run(self.TID)
            # Only minutes failed: the title arrived and was used.
            assert self._finished(telemetry) == ("error", "minutes.failed")
        assert email.call_args.args[2] == "Late Title"

    @pytest.mark.asyncio
    async def test_a_title_that_was_not_saved_is_reported_and_not_used(self, monkeypatch):
        from hmcts_otel.testing import capture_telemetry

        saved = MagicMock(id=self.TID, title=None)
        # The record is created (load_or_create_transcription); the title save fails.
        monkeypatch.setattr("transcribe_api.stt.pipeline.load_or_create_transcription", MagicMock(return_value=saved))
        monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription", MagicMock(side_effect=RuntimeError("db")))
        docx = AsyncMock(return_value="user-uploads/oid/transcript.docx")
        monkeypatch.setattr("transcribe_api.stt.pipeline._generate_and_upload_transcript_docx", docx)
        with capture_telemetry() as telemetry:
            await self._run(self.TID)
            assert self._finished(telemetry) == ("error", "title.failed")
        assert saved.title is None
        assert docx.call_args.args[2] == "Transcript"

    @pytest.mark.asyncio
    async def test_an_azure_speech_error_is_a_dependency_error_in_stage_and_job(self, monkeypatch):
        from fastapi import HTTPException
        from hmcts_otel import conventions as c
        from hmcts_otel.testing import capture_telemetry

        self._saved(monkeypatch, self.TID)
        # transcription.py raises 422 for any Azure error body, e.g. an invalid key (401).
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.transcribe_audio", AsyncMock(side_effect=HTTPException(status_code=422))
        )
        with capture_telemetry() as telemetry:
            with pytest.raises(HTTPException):
                await self._run(self.TID)
            assert self._finished(telemetry) == ("error", "transcription.failed")
            failed = telemetry.events("transcription.failed")[0]
            assert failed[c.OUTCOME] == "error"
            assert failed[c.ERROR_CODE] == "speech.request_failed"

    @pytest.mark.asyncio
    async def test_job_outcome_follows_the_stage_that_stopped_it(self, monkeypatch):
        from fastapi import HTTPException
        from hmcts_otel.testing import capture_telemetry

        self._saved(monkeypatch, self.TID)
        # A client-type rejection inside a stage (not the Azure call) stays a failure.
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.process_speakers_and_dialogue_entries",
            AsyncMock(side_effect=HTTPException(status_code=400)),
        )
        with capture_telemetry() as telemetry:
            with pytest.raises(HTTPException):
                await self._run(self.TID)
            assert self._finished(telemetry) == ("failure", "speaker_naming.failed")
