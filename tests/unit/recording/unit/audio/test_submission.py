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
