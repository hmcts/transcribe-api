"""DIAAT-276: end-to-end job tracing for the recording service.

Acceptance criteria covered:
* querying one identifier (the job ID) gives the ordered lifecycle of the journey;
* the trace identifies the final outcome and the component where a failure occurred;
* internal job IDs are related to Azure Speech job IDs;
* no audio, transcript or case content is exported.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from hmcts_otel import capture_trace_context, get_tracer
from hmcts_otel import conventions as c
from hmcts_otel.testing import TelemetryCapture, capture_telemetry

from transcribe_api.stt.batch_client import BatchSubmissionError
from transcribe_api.stt.polling_service import BatchPollingService, _PendingJob
from transcribe_api.domain.models_recording import BatchJobStatus, SpeechBatchJob
from transcribe_api.runtime.telemetry import (
    JOURNEY_UPLOAD,
    batch_job_id_from_url,
    configure_telemetry,
    upload_id_from_blob,
)

SPEECH_JOB_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SPEECH_URL = f"https://uksouth.api.cognitive.microsoft.com/speechtotext/v3.2/transcriptions/{SPEECH_JOB_ID}"
UPLOAD_ID = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
BLOB = f"uploads/00000000-0000-0000-0000-000000000001/{UPLOAD_ID}-hearing.wav"
SECRET = "the appellant " + "SECRET" + "CONTENT"  # built at runtime: never in a source line


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()  # registers the service schema and redaction rules
        yield capture


def _ordered(tel: TelemetryCapture, journey_id: str) -> list[str]:
    """What a support user gets from one KQL query on hmcts.journey.id, in order."""
    return [str(e[c.CUSTOM_EVENT_NAME]) for e in tel.events() if e.get(c.JOURNEY_ID) == journey_id]


def test_helpers():
    assert upload_id_from_blob(BLOB) == UPLOAD_ID
    assert upload_id_from_blob("elsewhere/x.wav") is None
    assert upload_id_from_blob(None) is None
    assert batch_job_id_from_url(SPEECH_URL) == SPEECH_JOB_ID


# Upload -----------------------------------------------------------------------------------


def _client_with_user(mocker):
    from transcribe_api.api.app import create_app
    from transcribe_api.domain.models_recording import User
    from transcribe_api.domain.auth.dependencies_recording import get_current_user

    manager = mocker.AsyncMock()
    manager.create_blob_from_file = mocker.AsyncMock(return_value=True)
    manager.build_blob_url = mocker.Mock(return_value="https://x/y.wav")
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("transcribe_api.api.routes_recording.AsyncAzureBlobManager", return_value=manager)
    mocker.patch("transcribe_api.api.routes_recording.normalize_to_wav", new=mocker.AsyncMock())
    app = create_app()
    user = mocker.Mock(spec=User)
    user.id = "00000000-0000-0000-0000-000000000001"
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_upload_is_a_journey_whose_id_is_in_the_blob_name(tel, mocker):
    response = _client_with_user(mocker).post(
        "/api/v1/uploads", files={"file": ("hearing.mp3", b"bytes", "audio/mpeg")}
    )
    assert response.status_code == 201
    upload_id = upload_id_from_blob(response.json()["blob_name"])
    assert upload_id is not None
    (done,) = tel.events("upload.completed")
    assert done[c.JOURNEY_TYPE] == JOURNEY_UPLOAD and done[c.JOURNEY_ID] == upload_id
    assert done["audio.format"] == "mp3"


def test_rejected_upload_has_a_controlled_failure_code(tel, mocker):
    response = _client_with_user(mocker).post("/api/v1/uploads", files={"file": ("notes.txt", b"bytes", "text/plain")})
    assert response.status_code == 422
    (failed,) = tel.events("upload.failed")
    assert failed[c.OUTCOME] == "failure"
    assert failed[c.ERROR_CODE] == "upload.unsupported_format"


# Submission --------------------------------------------------------------------------------


def _submit(mocker, *, error: Exception | None = None):
    from transcribe_api.stt import submission

    saved: list[SpeechBatchJob] = []

    def save(session, job):
        if job.id is None:
            job.id = uuid4()
        saved.append(job)
        return job

    mocker.patch.object(submission, "save_job", side_effect=save)
    mocker.patch.object(
        submission,
        "submit_batch_job",
        new=AsyncMock(side_effect=error) if error else AsyncMock(return_value=SPEECH_URL),
    )
    return submission, saved


async def test_submission_stores_trace_context_and_relates_the_speech_job(tel, mocker):
    submission, saved = _submit(mocker)
    with get_tracer().start_as_current_span("POST /api/v1/jobs") as request_span:
        job = await submission.submit_and_queue_batch_job(
            session=MagicMock(), audio_url="https://x/y.wav", audio_blob_path=BLOB
        )
    assert job.trace_parent and request_span.get_span_context().span_id != 0
    assert format(request_span.get_span_context().trace_id, "032x") in job.trace_parent

    job_id = str(job.id)
    assert _ordered(tel, job_id) == [
        "job.created",
        "speech_submission.started",
        "speech_submission.completed",
    ]
    done = tel.events("speech_submission.completed")[0]
    assert done[c.DEPENDENCY_REF] == SPEECH_JOB_ID
    assert done[c.JOURNEY_PARENT_ID] == UPLOAD_ID


async def test_rejected_submission_records_failure_and_component(tel, mocker):
    submission, _ = _submit(mocker, error=BatchSubmissionError("Azure said: " + SECRET))
    with pytest.raises(BatchSubmissionError):
        await submission.submit_and_queue_batch_job(session=MagicMock(), audio_url="https://x/y.wav")
    (finished,) = tel.events("job.finished")
    assert finished[c.OUTCOME] == "error"  # dependency failure: counted as an error
    assert finished[c.ERROR_CODE] == "speech.submission_rejected"
    assert tel.events("speech_submission.failed")[0][c.ERROR_CODE] == "speech.submission_rejected"
    tel.assert_no_prohibited_content(SECRET)


# Polling -----------------------------------------------------------------------------------


def _pending(job_id: UUID, trace_parent: str | None) -> _PendingJob:
    return _PendingJob(
        id=job_id,
        batch_job_url=SPEECH_URL,
        batch_job_status=BatchJobStatus.RUNNING,
        callback_url=None,
        caller_id=None,
        metadata_={},
        trace_parent=trace_parent,
        audio_blob_path=BLOB,
    )


def _service() -> BatchPollingService:
    service = BatchPollingService(webhook_secret_resolver=lambda caller_id: "")
    service._save_results = MagicMock()
    service._record_error = MagicMock()
    service._update_status = MagicMock()
    service._record_cleanup_failure = MagicMock()
    return service


async def test_whole_lifecycle_is_one_query_and_linked_to_the_request(tel, mocker):
    # Request: job created and submitted.
    submission, _ = _submit(mocker)
    with get_tracer().start_as_current_span("POST /api/v1/jobs") as request_span:
        job = await submission.submit_and_queue_batch_job(
            session=MagicMock(), audio_url="https://x/y.wav", audio_blob_path=BLOB
        )

    # Minutes later, in the poller (no request context).
    pending = _pending(job.id, job.trace_parent)
    base = "transcribe_api.stt.polling_service"
    with (
        patch(f"{base}.get_batch_job_status", new=AsyncMock(return_value={"status": "Succeeded"})),
        patch(f"{base}.get_batch_results", new=AsyncMock(return_value=[{"text": SECRET}])),
        patch(f"{base}.process_speakers", side_effect=lambda entries: entries),
        patch(f"{base}._resolve_model_display_name", new=AsyncMock(return_value=None)),
        patch(f"{base}.delete_batch_job", new=AsyncMock()),
    ):
        await _service()._process_job(pending)

    assert _ordered(tel, str(job.id)) == [
        "job.created",
        "speech_submission.started",
        "speech_submission.completed",
        "result_processing.started",
        "transcript.available",
        "job.finished",
        "result_processing.completed",
    ]
    finished = tel.events("job.finished")[0]
    assert finished[c.OUTCOME] == "success"
    processing = next(s for s in tel.finished_spans() if s.name == "result_processing")
    (link,) = processing.links
    assert link.context.trace_id == request_span.get_span_context().trace_id
    assert processing.attributes[c.DEPENDENCY_REF] == SPEECH_JOB_ID
    tel.assert_no_prohibited_content(SECRET)


async def test_azure_failure_identifies_the_component_without_its_message(tel):
    pending = _pending(uuid4(), None)
    service = _service()
    service._dispatch_failure = AsyncMock()
    status = {"status": "Failed", "properties": {"error": {"message": "decode failed: " + SECRET}}}
    base = "transcribe_api.stt.polling_service"
    with (
        patch(f"{base}.get_batch_job_status", new=AsyncMock(return_value=status)),
        patch(f"{base}.AsyncAzureBlobManager"),
        patch(f"{base}.delete_batch_job", new=AsyncMock()),
    ):
        await service._process_job(pending)

    assert _ordered(tel, str(pending.id)) == ["speech.job_failed", "job.finished"]
    assert tel.events("speech.job_failed")[0][c.DEPENDENCY_REF] == SPEECH_JOB_ID
    finished = tel.events("job.finished")[0]
    assert finished[c.OUTCOME] == "error" and finished[c.ERROR_CODE] == "speech.job_failed"
    tel.assert_no_prohibited_content(SECRET)


async def test_results_unavailable_is_attributed_to_result_processing(tel):
    from transcribe_api.stt.batch_client import BatchResultError

    pending = _pending(uuid4(), capture_trace_context())
    service = _service()
    service._dispatch_failure = AsyncMock()
    base = "transcribe_api.stt.polling_service"
    with (
        patch(f"{base}.get_batch_job_status", new=AsyncMock(return_value={"status": "Succeeded"})),
        patch(f"{base}.get_batch_results", new=AsyncMock(side_effect=BatchResultError("x"))),
    ):
        await service._process_job(pending)

    failed = tel.events("result_processing.failed")[0]
    assert failed[c.OUTCOME] == "error"
    assert failed[c.ERROR_CODE] == "speech.results_unavailable"
    assert tel.events("job.finished")[0][c.ERROR_CODE] == "speech.results_unavailable"


def test_storage_failure_on_upload_is_an_error(tel, mocker):
    client = _client_with_user(mocker)
    from transcribe_api.api import routes_recording

    manager = routes_recording.AsyncAzureBlobManager.return_value
    manager.create_blob_from_file.return_value = False
    response = client.post("/api/v1/uploads", files={"file": ("hearing.mp3", b"b", "audio/mpeg")})
    assert response.status_code == 502
    (failed,) = tel.events("upload.failed")
    assert failed[c.OUTCOME] == "error" and failed[c.ERROR_CODE] == "storage.write_failed"


async def test_client_error_during_submission_is_a_failure_like_its_stage(tel, mocker):
    from fastapi import HTTPException

    submission, _ = _submit(mocker, error=HTTPException(status_code=400))
    with pytest.raises(HTTPException):
        await submission.submit_and_queue_batch_job(session=MagicMock(), audio_url="https://x/y.wav")
    (finished,) = tel.events("job.finished")
    assert finished[c.OUTCOME] == "failure"
    assert tel.events("speech_submission.failed")[0][c.OUTCOME] == "failure"


async def test_no_speech_token_is_an_error_naming_the_auth(tel, mocker):
    from transcribe_api.stt.speech_auth import SpeechAuthError

    submission, saved = _submit(mocker, error=SpeechAuthError("no federated token"))
    with pytest.raises(SpeechAuthError):
        await submission.submit_and_queue_batch_job(session=MagicMock(), audio_url="https://x/y.wav")
    (finished,) = tel.events("job.finished")
    assert finished[c.OUTCOME] == "error" and finished[c.ERROR_CODE] == "speech.auth_failed"
    failed = tel.events("speech_submission.failed")[0]
    assert failed[c.OUTCOME] == "error" and failed[c.ERROR_CODE] == "speech.auth_failed"
    assert saved[-1].error_message == "Could not authenticate to Azure Speech"


def test_missing_ffmpeg_on_upload_is_an_error(tel, mocker):
    client = _client_with_user(mocker)
    from transcribe_api.api import routes_recording

    routes_recording.normalize_to_wav.side_effect = FileNotFoundError("ffmpeg")
    response = client.post("/api/v1/uploads", files={"file": ("hearing.mp3", b"b", "audio/mpeg")})
    assert response.status_code == 503
    (failed,) = tel.events("upload.failed")
    assert failed[c.OUTCOME] == "error" and failed[c.ERROR_CODE] == "upload.transcoder_unavailable"


@pytest.mark.parametrize(
    ("status", "results_error", "code"),
    [
        ("Failed", None, "speech.job_failed"),
        ("Succeeded", "unavailable", "speech.results_unavailable"),
        ("Succeeded", "error", "speech.results_error"),
    ],
)
async def test_a_job_is_finished_once_its_failed_state_is_stored(tel, status, results_error, code):
    """A failed write leaves the job to the next poll, which finishes it: once."""
    from transcribe_api.stt.batch_client import BatchResultError

    pending = _pending(uuid4(), capture_trace_context())
    service = _service()
    service._dispatch_failure = AsyncMock()
    service._record_error = MagicMock(side_effect=[RuntimeError("database down"), None])
    error = BatchResultError("x") if results_error == "unavailable" else RuntimeError("x")
    base = "transcribe_api.stt.polling_service"
    with (
        patch(f"{base}.get_batch_job_status", new=AsyncMock(return_value={"status": status})),
        patch(f"{base}.get_batch_results", new=AsyncMock(side_effect=error)),
        patch(f"{base}.AsyncAzureBlobManager"),
        patch(f"{base}.delete_batch_job", new=AsyncMock()),
    ):
        await service._process_job(pending)
        assert tel.events("job.finished") == []
        assert tel.events("speech.job_failed") == []
        service._dispatch_failure.assert_not_called()

        await service._process_job(pending)

    (finished,) = tel.events("job.finished")
    assert finished[c.OUTCOME] == "error" and finished[c.ERROR_CODE] == code
    assert len(tel.events("speech.job_failed")) == (1 if status == "Failed" else 0)
