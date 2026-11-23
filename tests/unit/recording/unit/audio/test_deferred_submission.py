"""DIAAT-408: creating a job without submitting it, and submitting a job that already exists
(stt/submission.py). The same events and failure handling as a job submitted in its request, with
``job.created`` recorded exactly once whichever way the job was submitted."""

from __future__ import annotations

import pytest
from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry
from sqlmodel import Session

from tests.unit.recording.unit.async_upload_helpers import USER_ID, new_engine
from transcribe_api.domain.models_recording import BatchJobStatus, JobStatus, SpeechBatchJob
from transcribe_api.runtime import settings_recording
from transcribe_api.runtime.telemetry import configure_telemetry
from transcribe_api.stt import submission
from transcribe_api.stt.audio_storage import job_wav_name
from transcribe_api.stt.batch_client import BatchSubmissionError

UPLOAD_ID = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
BLOB = f"uploads/{USER_ID}/{UPLOAD_ID}-hearing.wav"
SOURCE = f"incoming/{USER_ID}/{UPLOAD_ID}.mp3"


@pytest.fixture(autouse=True)
def mock_speech(monkeypatch):
    monkeypatch.setenv("SPEECH_BACKEND", "mock")
    settings_recording.get_settings.cache_clear()


@pytest.fixture
def session(tmp_path):
    engine = new_engine(tmp_path / "jobs.db")
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


def _events(tel, job) -> list[str]:
    return [str(e[c.CUSTOM_EVENT_NAME]) for e in tel.events() if e.get(c.JOURNEY_ID) == str(job.id)]


def test_a_deferred_job_is_stored_pending_with_its_source_and_nothing_is_sent_to_azure(session, tel, mocker):
    sent = mocker.patch.object(submission, "submit_batch_job", side_effect=AssertionError("sent to Azure"))

    job = submission.defer_batch_job(
        session,
        "https://client/ignored.wav",
        SOURCE,
        audio_blob_path=BLOB,
        user_id=USER_ID,
        idempotency_key="k",
        audio_duration_seconds=7.5,
        metadata={"audio_file_name": "hearing.mp3"},
    )

    sent.assert_not_called()
    stored = session.get(SpeechBatchJob, job.id)
    assert stored.status == JobStatus.PENDING
    assert (stored.source_blob_path, stored.audio_blob_path, stored.preparation_attempts) == (
        SOURCE,
        job_wav_name(BLOB, job.id),
        0,
    )
    assert stored.batch_job_id is None and stored.batch_job_status is None
    assert (stored.idempotency_key, stored.audio_duration_seconds, stored.user_id) == ("k", 7.5, USER_ID)
    # job.created, once, on the job's journey whose parent is the upload.
    assert _events(tel, job) == ["job.created"]
    assert tel.events("job.created")[0][c.JOURNEY_PARENT_ID] == UPLOAD_ID


async def test_an_existing_job_is_submitted_without_a_second_job_created(session, tel):
    job = submission.defer_batch_job(session, "https://x/y.wav", SOURCE, audio_blob_path=BLOB, user_id=USER_ID)

    submitted = await submission.submit_existing_job(session, job)

    assert submitted.status == JobStatus.SUBMITTED
    assert submitted.batch_job_status == BatchJobStatus.NOT_STARTED and submitted.batch_job_id
    assert _events(tel, job) == [
        "job.created",  # from defer_batch_job
        "speech_submission.started",
        "speech_submission.completed",
        "job.queued",
    ]


async def test_a_job_submitted_in_its_request_records_job_created_then_the_submission(session, tel):
    job = await submission.submit_and_queue_batch_job(
        session, audio_url="https://x/y.wav", audio_blob_path=BLOB, user_id=USER_ID
    )
    assert _events(tel, job) == [
        "job.created",
        "speech_submission.started",
        "speech_submission.completed",
        "job.queued",
    ]


async def test_a_refused_submission_of_an_existing_job_fails_it_as_a_request_would_and_releases_the_key(
    session, tel, mocker
):
    mocker.patch.object(submission, "submit_batch_job", side_effect=BatchSubmissionError("Azure said: secret"))
    job = submission.defer_batch_job(
        session, "https://x/y.wav", SOURCE, audio_blob_path=BLOB, user_id=USER_ID, idempotency_key="k"
    )

    with pytest.raises(BatchSubmissionError):
        await submission.submit_existing_job(session, job)

    session.expire_all()
    stored = session.get(SpeechBatchJob, job.id)
    assert stored.status == JobStatus.FAILED
    assert stored.error_message == "Azure batch transcription submission failed"
    assert stored.idempotency_key is None
    assert [e[c.ERROR_CODE] for e in tel.events("job.finished")] == ["speech.submission_rejected"]
