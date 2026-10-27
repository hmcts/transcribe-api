"""Unit tests for the batch submission pipeline (stt/submission.py)."""

from unittest.mock import patch

import pytest

from transcribe_api.domain.models_recording import JobStatus


@pytest.mark.asyncio
async def test_speech_credential_failure_marks_the_job_failed_with_a_clear_message(
    speech_workload_identity,
):
    from transcribe_api.stt.speech_auth import SpeechAuthError
    from transcribe_api.stt.submission import submit_and_queue_batch_job

    speech_workload_identity.error = RuntimeError("no federated token")
    saved = []

    def fake_save(session, job):
        saved.append((job.status, job.error_message))
        return job

    with (
        patch("transcribe_api.stt.submission.save_job", side_effect=fake_save),
        pytest.raises(SpeechAuthError),
    ):
        await submit_and_queue_batch_job(session=None, audio_url="https://audio")

    status, message = saved[-1]
    assert status == JobStatus.FAILED
    assert message == "Could not authenticate to Azure Speech"


def _record_saves(saved: list):
    def fake_save(session, job):
        saved.append((job.status, job.idempotency_key, job.audio_blob_path))
        return job

    return fake_save


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["batch_rejected", "speech_auth"])
async def test_speech_rejection_releases_the_idempotency_key_and_keeps_the_blob(error):
    # Speech never accepted the job, so a retry with the same key must create a
    # new job rather than get this failed one back (the key is unique per user,
    # ix_transcription_job_user_idempotency). The blob stays on the failed job:
    # a retry may resubmit the same blob_name.
    from transcribe_api.stt.batch_client import BatchSubmissionError
    from transcribe_api.stt.speech_auth import SpeechAuthError
    from transcribe_api.stt.submission import submit_and_queue_batch_job

    exc = (
        BatchSubmissionError("Batch job submission failed: HTTP 403 — Public access is disabled")
        if error == "batch_rejected"
        else SpeechAuthError("no federated token")
    )
    saved = []

    with (
        patch("transcribe_api.stt.submission.save_job", side_effect=_record_saves(saved)),
        patch("transcribe_api.stt.submission.submit_batch_job", side_effect=exc),
        pytest.raises(type(exc)),
    ):
        await submit_and_queue_batch_job(
            session=None,
            audio_url="https://audio",
            idempotency_key="retry-me",
            audio_blob_path="uploads/u/audio.wav",
        )

    assert saved[0] == (JobStatus.PENDING, "retry-me", "uploads/u/audio.wav")
    assert saved[-1] == (JobStatus.FAILED, None, "uploads/u/audio.wav")


@pytest.mark.asyncio
async def test_unexpected_submission_error_keeps_the_idempotency_key():
    # Deliberate: after a timeout or a failure past the submit call, Speech may
    # already be transcribing, so a same-key retry gets this job back rather
    # than starting a second transcription.
    from transcribe_api.stt.submission import submit_and_queue_batch_job

    saved = []

    with (
        patch("transcribe_api.stt.submission.save_job", side_effect=_record_saves(saved)),
        patch("transcribe_api.stt.submission.submit_batch_job", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        await submit_and_queue_batch_job(session=None, audio_url="https://audio", idempotency_key="k")

    assert saved[-1][:2] == (JobStatus.FAILED, "k")
