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

# Clear the get_settings cache so other test files can create their own Settings
# instances without getting our dummy-values cached result.
from transcribe_api.runtime.settings_dictation import get_settings as _gs  # noqa: E402
from transcribe_api.stt.pipeline import (  # noqa: E402
    generate_and_save_meeting_title,
    transcribe_and_generate_llm_output,
)

_gs.cache_clear()


@pytest.fixture(autouse=True)
def _patch_external(monkeypatch):
    """Patch all external dependencies for every test in this module."""
    monkeypatch.setattr("transcribe_api.stt.pipeline.load_or_create_transcription", MagicMock())
    monkeypatch.setattr("transcribe_api.stt.pipeline.get_transcription_title", MagicMock(return_value=None))
    monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_title", MagicMock(return_value=True))
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
        monkeypatch.setattr("transcribe_api.stt.pipeline.load_or_create_transcription", mock_save)

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
        # The user sees the type, never the exception text (it can quote the response).
        assert job_arg.error_message.startswith("Transcription failed (RuntimeError)")
        assert "Azure error" not in job_arg.error_message

    async def test_transcription_failure_log_is_type_only(self, monkeypatch, caplog):
        # The stage and the job's background span record the exception: this log
        # must not add a third exception record (PR review).
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.transcribe_audio",
            AsyncMock(side_effect=RuntimeError("Azure error")),
        )
        monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_job", MagicMock())

        with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
            await transcribe_and_generate_llm_output(
                user_upload_blob_storage_file_key="blob",
                user_id=uuid4(),
                user_email="user@example.com",
                azure_user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            )

        (record,) = [r for r in caplog.records if r.getMessage() == "Transcription failed (RuntimeError)"]
        assert record.exc_info is None
        assert "exception.stacktrace" not in record.__dict__

    async def test_a_failed_error_record_save_is_raised_so_the_poller_retries(self, monkeypatch, caplog):
        """A 422 (no speech, audio rejected) whose error record cannot be saved must not
        reach the poller as a 422, which skips the upload for good with no record:
        the save's failure is raised (retried), chained to the transcription's."""
        from fastapi import HTTPException
        from hmcts_otel import client_failure_code

        rejected = HTTPException(status_code=422, detail="No speech detected")
        monkeypatch.setattr("transcribe_api.stt.pipeline.transcribe_audio", AsyncMock(side_effect=rejected))
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.save_transcription_job", MagicMock(side_effect=OSError("database down"))
        )

        with caplog.at_level("ERROR"), pytest.raises(OSError, match="database down") as raised:
            await transcribe_and_generate_llm_output(
                user_upload_blob_storage_file_key="blob",
                user_id=uuid4(),
                user_email="user@example.com",
                azure_user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            )

        assert raised.value.__cause__ is rejected
        assert client_failure_code(raised.value) is None  # the poller retries it
        (record,) = [
            r for r in caplog.records if r.getMessage() == "The failed transcription job could not be saved (OSError)"
        ]
        assert record.exc_info is None
        assert "exception.stacktrace" not in record.__dict__

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
    @pytest.mark.parametrize("named_as", ["6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f", "6F1C2D3E4B5A4C6D8E7F9A0B1C2D3E4F"])
    async def test_a_record_save_failure_is_in_the_transcriptions_journey(self, monkeypatch, named_as):
        """The upload-job-failures alert counts this error: it carries the journey of
        the transcription the job was for (the ID the save would have used, canonical
        form), so it can be traced to its upload."""
        from hmcts_otel import conventions as c
        from hmcts_otel.testing import capture_telemetry

        from transcribe_api.runtime.telemetry import JOURNEY_TRANSCRIPTION

        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.load_or_create_transcription", MagicMock(side_effect=RuntimeError("db"))
        )
        with capture_telemetry() as telemetry:
            with pytest.raises(RuntimeError):
                await self._run(named_as)
            (finished,) = telemetry.events("job.finished")
        assert (finished[c.OUTCOME], finished[c.ERROR_CODE]) == ("error", "job_record.failed")
        assert finished[c.JOURNEY_ID] == self.TID
        assert finished[c.JOURNEY_TYPE] == JOURNEY_TRANSCRIPTION  # as the rest of the job

    @pytest.mark.asyncio
    async def test_a_record_save_failure_without_an_id_has_no_journey(self, monkeypatch):
        """A file name that is not a UUID: the ID would be generated by the save."""
        from hmcts_otel import conventions as c
        from hmcts_otel.testing import capture_telemetry

        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.load_or_create_transcription", MagicMock(side_effect=RuntimeError("db"))
        )
        with capture_telemetry() as telemetry:
            with pytest.raises(RuntimeError):
                await self._run(None)
            (finished,) = telemetry.events("job.finished")
        assert finished[c.ERROR_CODE] == "job_record.failed"
        assert c.JOURNEY_ID not in finished

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
        monkeypatch.setattr("transcribe_api.stt.pipeline.load_or_create_transcription", MagicMock(return_value=saved))
        # The record is created; the title update fails.
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.save_transcription_title", MagicMock(side_effect=RuntimeError("db"))
        )
        docx = AsyncMock(return_value="user-uploads/oid/transcript.docx")
        monkeypatch.setattr("transcribe_api.stt.pipeline._generate_and_upload_transcript_docx", docx)
        with capture_telemetry() as telemetry:
            await self._run(self.TID)
            assert self._finished(telemetry) == ("error", "title.failed")
        assert saved.title is None
        assert docx.call_args.args[2] == "Transcript"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 502])
    async def test_an_azure_speech_error_is_a_dependency_error_in_stage_and_job(self, monkeypatch, status):
        from fastapi import HTTPException
        from hmcts_otel import conventions as c
        from hmcts_otel.testing import capture_telemetry

        self._saved(monkeypatch, self.TID)
        # transcription.py raises 502 for auth, quota, throttling and outages (e.g. an
        # invalid key, 401), and 500 when it is misconfigured or Azure's response lacks phrases.
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.transcribe_audio",
            AsyncMock(side_effect=HTTPException(status_code=status)),
        )
        with capture_telemetry() as telemetry:
            with pytest.raises(HTTPException):
                await self._run(self.TID)
            assert self._finished(telemetry) == ("error", "transcription.failed")
            failed = telemetry.events("transcription.failed")[0]
            assert failed[c.OUTCOME] == "error"
            assert failed[c.ERROR_CODE] == "speech.request_failed"

    @pytest.mark.asyncio
    async def test_audio_rejected_by_azure_is_a_failure_in_stage_and_job(self, monkeypatch):
        from fastapi import HTTPException
        from hmcts_otel import conventions as c
        from hmcts_otel.testing import capture_telemetry

        self._saved(monkeypatch, self.TID)
        # transcription.py raises 422 when Azure rejected the audio or the request: the
        # user's file, not a service error, so it stays out of error rates and alerts.
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.transcribe_audio", AsyncMock(side_effect=HTTPException(status_code=422))
        )
        with capture_telemetry() as telemetry:
            with pytest.raises(HTTPException):
                await self._run(self.TID)
            assert self._finished(telemetry) == ("failure", "transcription.failed")
            failed = telemetry.events("transcription.failed")[0]
            assert failed[c.OUTCOME] == "failure"
            assert failed[c.ERROR_CODE] == "speech.audio_rejected"

    @pytest.mark.asyncio
    async def test_audio_with_no_speech_is_a_failure_with_its_own_code_and_message(self, monkeypatch):
        """Silent or speech-free audio: Azure transcribed it and found nothing. The
        user's recording: a failure (out of error rates and the upload-job-failures
        alert), with a reason the user can act on rather than "please try again"."""
        from hmcts_otel import conventions as c
        from hmcts_otel.testing import capture_telemetry

        from transcribe_api.stt.fast_client import NoSpeechDetectedError

        self._saved(monkeypatch, self.TID)
        saved_jobs = []
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.save_transcription_job", lambda job, **_: saved_jobs.append(job)
        )
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.transcribe_audio", AsyncMock(side_effect=NoSpeechDetectedError())
        )
        with capture_telemetry() as telemetry:
            with pytest.raises(NoSpeechDetectedError):
                await self._run(self.TID)
            assert self._finished(telemetry) == ("failure", "transcription.failed")
            failed = telemetry.events("transcription.failed")[0]
            assert failed[c.OUTCOME] == "failure"
            assert failed[c.ERROR_CODE] == "speech.no_speech_detected"
        (job,) = saved_jobs
        assert "no speech was detected" in job.error_message
        assert "try again" not in job.error_message

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


@pytest.mark.asyncio
async def test_job_error_message_never_holds_the_exception_text(monkeypatch):
    """Shown to the user and stored: the exception text can quote transcript or an
    Azure response, so only its type is kept (as in logs and blob metadata)."""
    secret = "the appellant " + "said X"
    saved_jobs = []
    monkeypatch.setattr(
        "transcribe_api.stt.pipeline.load_or_create_transcription",
        MagicMock(return_value=MagicMock(id=uuid4(), title=None)),
    )
    monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_job", lambda job, **_: saved_jobs.append(job))
    monkeypatch.setattr("transcribe_api.stt.pipeline.transcribe_audio", AsyncMock(side_effect=RuntimeError(secret)))
    with pytest.raises(RuntimeError):
        await transcribe_and_generate_llm_output(
            user_upload_blob_storage_file_key="user-uploads/oid/x.mp4",
            user_id=uuid4(),
            user_email="judge@justice.gov.uk",
            azure_user_id="oid",
        )
    (job,) = saved_jobs
    assert secret not in job.error_message
    assert "RuntimeError" in job.error_message


def test_minutes_error_message_never_holds_the_exception_text():
    from transcribe_api.domain.interface_dictation import create_error_minute_version

    version = create_error_minute_version(str(uuid4()), uuid4(), ValueError("prompt said: the appellant"))
    assert "appellant" not in version.error_message
    assert "ValueError" in version.error_message


@pytest.mark.asyncio
async def test_an_ownership_conflict_is_a_failure_outside_any_journey(monkeypatch):
    """SEC-1 x DIAAT-276: a file name naming another user's transcription is rejected
    (outcome failure, its own code) and never written into that user's journey."""
    from hmcts_otel import conventions as c
    from hmcts_otel.testing import capture_telemetry

    from transcribe_api.domain.exceptions import OwnershipConflictError

    victim = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    monkeypatch.setattr(
        "transcribe_api.stt.pipeline.load_or_create_transcription",
        MagicMock(side_effect=OwnershipConflictError("Transcription")),
    )
    with capture_telemetry() as telemetry:
        with pytest.raises(OwnershipConflictError):
            await transcribe_and_generate_llm_output(
                user_upload_blob_storage_file_key=f"user-uploads/oid/{victim}.mp4",
                user_id=uuid4(),
                user_email="judge@justice.gov.uk",
                azure_user_id="oid",
                transcription_id=victim,
            )
        (finished,) = telemetry.events("job.finished")
        assert finished[c.OUTCOME] == "failure"
        assert finished[c.ERROR_CODE] == "job.ownership_conflict"
        assert not [e for e in telemetry.events() if e.get(c.JOURNEY_ID) == victim]
        # Documented choice: no journey at all (the only ID is the victim's).
        assert c.JOURNEY_ID not in finished


@pytest.mark.asyncio
async def test_a_title_saved_before_a_later_error_counts_as_saved(monkeypatch):
    """The commit succeeded and only the refresh failed: the title is kept and the
    job does not report title.failed (stage and job agree)."""
    from hmcts_otel.testing import capture_telemetry

    from transcribe_api.domain.models_dictation import Transcription
    from transcribe_api.stt.pipeline import _generate_and_save_title

    monkeypatch.setattr("transcribe_api.stt.pipeline.generate_meeting_title", AsyncMock(return_value="Bail hearing"))
    monkeypatch.setattr(
        "transcribe_api.stt.pipeline.save_transcription_title", MagicMock(side_effect=RuntimeError("after commit"))
    )
    monkeypatch.setattr("transcribe_api.stt.pipeline.get_transcription_title", MagicMock(return_value="Bail hearing"))
    transcription = Transcription(id=uuid4())
    with capture_telemetry() as telemetry:
        assert await _generate_and_save_title([], transcription, uuid4()) is True
        assert telemetry.events("title_generation.completed")
    assert transcription.title == "Bail hearing"


@pytest.mark.asyncio
async def test_an_empty_title_is_a_title_failure_in_stage_and_job(monkeypatch):
    from hmcts_otel import conventions as c
    from hmcts_otel.testing import capture_telemetry

    from transcribe_api.domain.models_dictation import Transcription
    from transcribe_api.stt.pipeline import _generate_and_save_title

    monkeypatch.setattr("transcribe_api.stt.pipeline.generate_meeting_title", AsyncMock(return_value="  "))
    save = MagicMock()
    monkeypatch.setattr("transcribe_api.stt.pipeline.load_or_create_transcription", save)
    with capture_telemetry() as telemetry:
        assert await _generate_and_save_title([], Transcription(id=uuid4()), uuid4()) is False
        assert telemetry.events("title_generation.failed")[0][c.ERROR_CODE] == "title.failed"
    save.assert_not_called()


@pytest.mark.asyncio
async def test_the_title_update_writes_only_the_title(monkeypatch):
    """The job's copy of the row may be stale (the user may have changed it while
    the job ran): only the title column is updated, for this user's transcription."""
    from transcribe_api.domain.models_dictation import Transcription
    from transcribe_api.stt.pipeline import _generate_and_save_title

    monkeypatch.setattr("transcribe_api.stt.pipeline.generate_meeting_title", AsyncMock(return_value="Bail hearing"))
    whole_row_save = MagicMock()
    monkeypatch.setattr("transcribe_api.stt.pipeline.load_or_create_transcription", whole_row_save)
    title_only = MagicMock(return_value=True)
    monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_title", title_only)
    transcription, user_id = Transcription(id=uuid4()), uuid4()
    assert await _generate_and_save_title([], transcription, user_id) is True
    title_only.assert_called_once_with(transcription.id, user_id, "Bail hearing", None)
    whole_row_save.assert_not_called()


@pytest.mark.asyncio
async def test_a_rename_made_while_the_job_ran_is_kept(monkeypatch):
    from transcribe_api.domain.models_dictation import Transcription
    from transcribe_api.stt.pipeline import _generate_and_save_title

    monkeypatch.setattr("transcribe_api.stt.pipeline.generate_meeting_title", AsyncMock(return_value="Generated"))
    monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_title", MagicMock(return_value=False))
    monkeypatch.setattr("transcribe_api.stt.pipeline.get_transcription_title", MagicMock(return_value="Judge's name"))
    transcription = Transcription(id=uuid4())
    assert await _generate_and_save_title([], transcription, uuid4()) is True
    assert transcription.title == "Judge's name"


@pytest.mark.asyncio
async def test_a_title_cleared_while_the_job_ran_is_kept(monkeypatch):
    """An empty stored title is the user's choice, not a missing transcription."""
    from transcribe_api.domain.models_dictation import Transcription
    from transcribe_api.stt.pipeline import _generate_and_save_title

    monkeypatch.setattr("transcribe_api.stt.pipeline.generate_meeting_title", AsyncMock(return_value="Generated"))
    monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_title", MagicMock(return_value=False))
    monkeypatch.setattr("transcribe_api.stt.pipeline.get_transcription_title", MagicMock(return_value=""))
    transcription = Transcription(id=uuid4())
    assert await _generate_and_save_title([], transcription, uuid4()) is True
    assert transcription.title == ""


def test_user_error_messages_tell_permanent_failures_from_transient_ones():
    from fastapi import HTTPException

    from transcribe_api.domain.interface_dictation import user_error_message

    azure = user_error_message(HTTPException(status_code=422, detail="Azure: the appellant said"), "Transcription")
    assert "supported audio format" in azure
    assert "unlikely to help" in azure
    assert "appellant" not in azure  # never the exception's text

    other_client = user_error_message(HTTPException(status_code=413, detail="x"), "Transcription")
    assert "error 413" in other_client
    assert "unlikely to help" in other_client

    transient = user_error_message(TimeoutError("db"), "Transcription")
    assert transient.startswith("Transcription failed (TimeoutError). Please try again")

    no_dialogue = user_error_message(HTTPException(status_code=400), "Minutes generation")
    assert "no transcript" in no_dialogue


@pytest.mark.asyncio
class TestTranscriptProvenance:
    """DIAAT-286: the transcript's job row records what produced it, in the same insert."""

    async def test_the_transcript_job_records_the_speech_configuration_and_dictionary(self, monkeypatch):
        from importlib import metadata

        from transcribe_api.domain.provenance_dictation import Provenance

        monkeypatch.setattr("transcribe_api.runtime.version.APP_VERSION", "9.9.9-release")
        settings = MagicMock(AZURE_SPEECH_ENDPOINT="https://uksouth.api.cognitive.microsoft.com")
        monkeypatch.setattr("transcribe_api.stt.fast_client.get_settings", lambda: settings)
        mock_save_job = MagicMock()
        monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_job", mock_save_job)

        await transcribe_and_generate_llm_output(
            user_upload_blob_storage_file_key="user-uploads/user@example.com/file.wav",
            user_id=uuid4(),
            user_email="user@example.com",
            azure_user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            transcription_id=str(uuid4()),
        )

        job = mock_save_job.call_args_list[0].args[0]  # the job holding the transcript
        provenance = Provenance.model_validate(job.provenance)
        assert provenance.output == "transcript"
        assert provenance.speech.endpoint_host == "uksouth.api.cognitive.microsoft.com"
        assert provenance.speech.region == "uksouth"
        assert provenance.speech.api_version == "2024-11-15"
        assert provenance.speech.locales == ["en-GB"]
        assert provenance.speech.diarization is True
        assert [(s.name, s.version) for s in provenance.post_processing] == [
            ("british_spelling", metadata.version("uwotm8"))
        ]
        assert provenance.application_release == "9.9.9-release"
        assert provenance.template is None

    async def test_a_failed_transcription_records_no_provenance(self, monkeypatch):
        monkeypatch.setattr(
            "transcribe_api.stt.pipeline.transcribe_audio", AsyncMock(side_effect=RuntimeError("Azure error"))
        )
        mock_save_job = MagicMock()
        monkeypatch.setattr("transcribe_api.stt.pipeline.save_transcription_job", mock_save_job)
        with pytest.raises(RuntimeError):
            await transcribe_and_generate_llm_output(
                user_upload_blob_storage_file_key="blob",
                user_id=uuid4(),
                user_email="user@example.com",
                azure_user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            )
        assert mock_save_job.call_args.args[0].provenance is None  # no output, nothing produced it
