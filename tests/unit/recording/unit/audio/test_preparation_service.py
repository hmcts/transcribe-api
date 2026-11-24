"""DIAAT-408: the worker's preparation of uploads the API accepted without converting them
(stt/preparation_service.py).

Real ffmpeg on real small files, the local-directory storage, SQLite for the job table and the mock
Speech backend: the whole of "claim, convert, store, submit, tidy up" runs through the real code.
Only the failures that need a broken storage or a crash are simulated.
"""

from __future__ import annotations

import asyncio
import wave
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry
from sqlalchemy import update
from sqlmodel import Session

from tests.unit.recording.unit.async_upload_helpers import add_job, new_engine, reload_job, tone
from transcribe_api.domain import interface_recording as jobs
from transcribe_api.domain.models_recording import BatchJobStatus, JobStatus, SpeechBatchJob
from transcribe_api.runtime import settings_dictation, settings_recording
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import configure_telemetry
from transcribe_api.stt import audio_storage, local_storage, submission
from transcribe_api.stt import preparation_service as prep
from transcribe_api.stt.batch_client import BatchSubmissionError
from transcribe_api.stt.preparation_service import PreparationService


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "http://api.test")
    monkeypatch.setenv("SPEECH_BACKEND", "mock")
    settings_recording.get_settings.cache_clear()
    settings_dictation.get_settings.cache_clear()


@pytest.fixture
def engine(mocker, tmp_path):
    engine = new_engine(tmp_path / "jobs.db")
    mocker.patch.object(prep, "get_engine", new=lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


@pytest.fixture
def fast_retries(mocker):
    """No wait before an attempt is repeated."""
    mocker.patch.object(get_settings(), "PREPARE_RETRY_BACKOFF_SECONDS", 0)


@pytest.fixture
def no_azure(mocker):
    return mocker.patch.object(submission, "submit_batch_job", side_effect=AssertionError("Azure was called"))


def _waiting(engine, body: bytes | None = None, **fields) -> SpeechBatchJob:
    """A deferred job with its original in storage."""
    job = add_job(engine, **fields)
    local_storage.save(tone(".mp3", 3.0) if body is None else body, job.source_blob_path)
    return job


async def _prepare_all(service: PreparationService) -> int:
    started = await service.poll_once()
    await service.wait_idle()
    return started


def _exists(blob_name: str) -> bool:
    try:
        local_storage.size(blob_name)
    except FileNotFoundError:
        return False
    return True


def _names(tel, job_id) -> list[str]:
    return [str(e[c.CUSTOM_EVENT_NAME]) for e in tel.events() if e.get(c.JOURNEY_ID) == str(job_id)]


# The whole path --------------------------------------------------------------------------------


async def test_a_waiting_job_is_converted_stored_submitted_and_its_original_removed(engine, tel):
    job = _waiting(engine)
    original = job.source_blob_path

    assert await _prepare_all(PreparationService()) == 1

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.SUBMITTED
    assert row.batch_job_status == BatchJobStatus.NOT_STARTED and row.batch_job_id
    assert row.preparation_attempts == 1 and row.error_message is None
    # The WAV is where the upload said it would be, 16 kHz mono, as long as the recording.
    wav_path = local_storage.path_of(job.audio_blob_path)
    with wave.open(str(wav_path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert wav.getnframes() / wav.getframerate() == pytest.approx(3.0, abs=0.2)
    assert not _exists(original)
    # Azure is sent the URL of the stored WAV, built by the server: not what the client sent.
    assert row.audio_url.startswith("http://api.test/api/v1/local-audio/uploads/")
    assert "client.example" not in row.audio_url
    assert row.audio_duration_seconds == pytest.approx(3.0, abs=0.2)


async def test_the_events_are_the_job_journeys_with_one_job_created_never_a_second(engine, tel):
    job = _waiting(engine)
    await _prepare_all(PreparationService())

    names = _names(tel, job.id)
    assert names == [
        "preparation.started",
        "preparation.completed",
        "speech_submission.started",
        "speech_submission.completed",
        "job.queued",
    ]
    done = tel.events("preparation.completed")[0]
    assert done["audio.duration_seconds"] == pytest.approx(3.0, abs=0.2)
    assert done[c.JOURNEY_PARENT_ID] == str(job.audio_blob_path.split("/")[2][:36])  # the upload's id
    assert tel.events("job.created") == []  # POST /jobs recorded it when it created the job


async def test_the_clients_audio_duration_is_kept_when_it_was_given(engine):
    job = _waiting(engine, audio_duration_seconds=99.0)
    await _prepare_all(PreparationService())
    assert reload_job(engine, job.id).audio_duration_seconds == 99.0


async def test_nothing_is_done_while_no_job_is_waiting(engine, no_azure):
    add_job(engine, source_blob_path=None)  # a job submitted by its request
    add_job(engine, status=JobStatus.SUBMITTED, batch_job_id="x", batch_job_url="https://x/y")
    add_job(engine, status=JobStatus.FAILED)
    assert await _prepare_all(PreparationService()) == 0


# What cannot be prepared -----------------------------------------------------------------------


async def test_a_file_ffmpeg_cannot_decode_fails_the_job_with_the_uploads_sentence(engine, tel, no_azure):
    job = _waiting(engine, body=b"this is not audio at all " * 100, idempotency_key="k-1")

    await _prepare_all(PreparationService())

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED
    assert row.error_message == "The audio file could not be decoded. It may be corrupt or in an unsupported format."
    assert row.idempotency_key is None and row.batch_job_id is None
    assert not _exists(row.audio_blob_path) and not _exists(job.source_blob_path)
    (failed,) = tel.events("preparation.failed")
    assert failed[c.ERROR_CODE] == "upload.undecodable" and failed[c.OUTCOME] == "failure"
    (finished,) = tel.events("job.finished")
    assert finished[c.ERROR_CODE] == "upload.undecodable" and finished[c.OUTCOME] == "failure"
    assert finished[c.JOURNEY_ID] == str(job.id)


async def test_a_recording_over_the_limit_by_the_exact_check_fails_the_job_with_the_uploads_sentence(
    engine, tel, no_azure, mocker
):
    mocker.patch.object(get_settings(), "MAX_AUDIO_DURATION_SECONDS", 60)  # 4 hours, scaled down
    job = _waiting(engine, body=tone(".mp3", 125.0))

    await _prepare_all(PreparationService())

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED
    assert row.error_message == "The recording is 2 minutes long. Recordings can be at most 1 minute."
    assert not _exists(row.audio_blob_path) and not _exists(job.source_blob_path)
    (failed,) = tel.events("preparation.failed")
    assert failed[c.ERROR_CODE] == "upload.too_long"
    assert failed["audio.duration_seconds"] == pytest.approx(125.0, abs=1.0)
    assert [e[c.ERROR_CODE] for e in tel.events("job.finished")] == ["upload.too_long"]


async def test_with_the_flags_off_a_job_over_azures_240_minutes_is_failed_with_the_4_hour_sentence(
    engine, no_azure, mocker, monkeypatch
):
    # DIAAT-408: only both flags on let a recording over 240 minutes through (it is then split:
    # tests/unit/recording/unit/audio/test_multipart_preparation.py). With either off this is the check
    # that has always been, and nothing reaches Azure.
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", "true")
    monkeypatch.setenv("LONG_RECORDINGS_ENABLED", "false")
    settings_dictation.get_settings.cache_clear()
    mocker.patch.object(prep, "wav_duration_seconds", return_value=5 * 3600.0)
    job = _waiting(engine)

    await _prepare_all(PreparationService())

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED
    assert row.error_message == "The recording is 5 hours long. Recordings can be at most 4 hours."


async def test_an_original_that_is_not_in_storage_fails_the_job(engine, tel, no_azure):
    job = add_job(engine)  # no blob stored

    await _prepare_all(PreparationService())

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED
    assert row.error_message == "The uploaded recording could not be found. Please upload it again."
    (failed,) = tel.events("preparation.failed")
    assert failed[c.ERROR_CODE] == "preparation.source_missing"


async def test_a_refusal_by_azure_speech_leaves_the_job_failed_with_the_usual_sentence(engine, tel, mocker):
    mocker.patch.object(submission, "submit_batch_job", side_effect=BatchSubmissionError("Azure said: secret detail"))
    job = _waiting(engine, idempotency_key="k-2")

    await _prepare_all(PreparationService())

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED
    assert row.error_message == "Azure batch transcription submission failed"  # never Azure's text
    assert row.idempotency_key is None
    assert not _exists(job.source_blob_path)  # settled: the original goes
    assert _exists(row.audio_blob_path)  # and the converted audio stays on the failed job, as for a request
    assert tel.events("preparation.completed") and tel.events("speech_submission.failed")


# Storage errors: retried, then failed ------------------------------------------------------------


class FailingStorage(audio_storage.LocalAudioStorage):
    """The local storage, with a step that fails the first ``failures`` times it is asked."""

    def __init__(self, step: str, failures: int) -> None:
        self.step, self.failures, self.calls = step, failures, 0

    def _maybe_fail(self, step: str) -> None:
        if step == self.step:
            self.calls += 1
            if self.calls <= self.failures:
                raise OSError("storage unavailable")

    async def download(self, blob_name, destination):
        self._maybe_fail("download")
        await super().download(blob_name, destination)

    async def upload(self, path, blob_name, *, concurrency=None):
        self._maybe_fail("upload")
        return await super().upload(path, blob_name, concurrency=concurrency)


@pytest.mark.parametrize("step", ["download", "upload"])
async def test_a_storage_error_is_retried_and_then_fails_the_job(engine, tel, fast_retries, no_azure, step):
    job = _waiting(engine)
    service = PreparationService(storage=FailingStorage(step, failures=99))

    for attempt in (1, 2):
        await _prepare_all(service)
        row = reload_job(engine, job.id)
        assert (row.status, row.preparation_attempts, row.error_message) == (JobStatus.PENDING, attempt, None)
    assert [e["job.retry.attempt"] for e in tel.events("job.retried")] == [1, 2]
    assert {e["job.retry.operation"] for e in tel.events("job.retried")} == {"job.prepare"}

    await _prepare_all(service)  # the third attempt, the last
    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED and row.preparation_attempts == 3
    assert row.error_message == "The recording could not be prepared for transcription. Please upload it again."
    assert [e[c.ERROR_CODE] for e in tel.events("preparation.failed")] == ["preparation.storage_failed"] * 3
    assert {e[c.OUTCOME] for e in tel.events("preparation.failed")} == {"error"}  # ours: an error, not the file's fault
    (finished,) = tel.events("job.finished")
    assert finished[c.OUTCOME] == "error" and finished[c.ERROR_CODE] == "preparation.storage_failed"
    assert await _prepare_all(service) == 0  # nothing more is tried


async def test_a_storage_error_that_passes_does_not_fail_the_job(engine, fast_retries):
    job = _waiting(engine)
    service = PreparationService(storage=FailingStorage("download", failures=1))

    await _prepare_all(service)
    assert reload_job(engine, job.id).status == JobStatus.PENDING
    await _prepare_all(service)

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.SUBMITTED and row.preparation_attempts == 2


async def test_a_job_waits_its_backoff_before_the_next_attempt(engine, mocker):
    mocker.patch.object(get_settings(), "PREPARE_RETRY_BACKOFF_SECONDS", 600)
    job = _waiting(engine)
    service = PreparationService(storage=FailingStorage("download", failures=99))
    await _prepare_all(service)
    assert reload_job(engine, job.id).preparation_attempts == 1
    assert await _prepare_all(service) == 0  # 10 minutes have not passed


async def test_a_store_that_reports_failure_is_retried_like_an_error(engine, fast_retries):
    class Refuses(audio_storage.LocalAudioStorage):
        async def upload(self, path, blob_name, *, concurrency=None):
            return False

    job = _waiting(engine)
    await _prepare_all(PreparationService(storage=Refuses()))
    row = reload_job(engine, job.id)
    assert (row.status, row.preparation_attempts) == (JobStatus.PENDING, 1)


async def test_a_missing_ffmpeg_is_retried_not_blamed_on_the_file(engine, tel, fast_retries, mocker):
    mocker.patch.object(prep, "normalize_to_wav", side_effect=FileNotFoundError(2, "No such file", "ffmpeg"))
    job = _waiting(engine)
    await _prepare_all(PreparationService())
    assert reload_job(engine, job.id).status == JobStatus.PENDING
    assert tel.events("preparation.failed")[0][c.ERROR_CODE] == "preparation.transcoder_unavailable"


async def test_an_unexpected_error_is_retried_too(engine, fast_retries, mocker):
    mocker.patch.object(prep, "wav_duration_seconds", side_effect=RuntimeError("boom"))
    job = _waiting(engine)
    await _prepare_all(PreparationService())
    assert reload_job(engine, job.id).status == JobStatus.PENDING


# Crashes and restarts ----------------------------------------------------------------------------


def _expire_lease(engine, job_id, minutes: int = 10) -> None:
    with Session(engine) as session:
        session.execute(
            update(SpeechBatchJob)
            .where(SpeechBatchJob.id == job_id)
            .values(updated_datetime=datetime.now(UTC) - timedelta(minutes=minutes))
        )
        session.commit()


async def test_a_worker_that_crashed_mid_preparation_is_taken_over_once_its_lease_has_run_out(engine, tel):
    job = _waiting(engine)
    # A worker claims the job and dies: it never renews, releases or finishes it.
    with Session(engine) as session:
        claimed = jobs.claim_job_for_preparation(session, max_attempts=3, lease_seconds=300)
    assert claimed.preparation_attempts == 1

    service = PreparationService()
    assert await _prepare_all(service) == 0  # it may still be alive
    _expire_lease(engine, job.id)
    assert await _prepare_all(service) == 1  # it is not

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.SUBMITTED and row.preparation_attempts == 2
    assert not _exists(job.source_blob_path) and _exists(job.audio_blob_path)


async def test_a_job_whose_last_attempt_crashed_is_failed_not_retried_for_ever(engine, tel, no_azure):
    job = _waiting(engine, preparation_attempts=3, updated_datetime=datetime.now(UTC) - timedelta(minutes=10))

    assert await _prepare_all(PreparationService()) == 0

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED and row.preparation_attempts == 3
    assert row.error_message == "The recording could not be prepared for transcription. Please upload it again."
    (finished,) = tel.events("job.finished")
    assert finished[c.ERROR_CODE] == "preparation.exhausted" and finished[c.JOURNEY_ID] == str(job.id)


async def test_a_job_on_its_last_attempt_is_not_failed_while_its_lease_holds(engine):
    job = _waiting(engine, preparation_attempts=3, updated_datetime=datetime.now(UTC))
    await _prepare_all(PreparationService())
    assert reload_job(engine, job.id).status == JobStatus.PENDING


async def test_a_worker_that_is_stopped_hands_its_job_back_at_once(engine):
    started, never = asyncio.Event(), asyncio.Event()

    async def convert(input_path, wav_path):
        started.set()
        await never.wait()

    job = _waiting(engine)
    service = PreparationService()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(prep, "normalize_to_wav", convert)
        await service.poll_once()
        await asyncio.wait_for(started.wait(), 10)
        for task in list(service._running):
            task.cancel()  # SIGTERM: the loop cancels what it runs
        await asyncio.gather(*service._running, return_exceptions=True)

    row = reload_job(engine, job.id)
    assert (row.status, row.preparation_attempts) == (JobStatus.PENDING, 1)
    # The next start claims it straight away (no wait for the lease), as its second attempt.
    with Session(engine) as session:
        again = jobs.claim_job_for_preparation(session, max_attempts=3, lease_seconds=300)
    assert again is not None and again.id == job.id and again.preparation_attempts == 2


async def test_two_workers_never_prepare_the_same_job(engine):
    job = _waiting(engine)
    with Session(engine) as one, Session(engine) as two:
        first = jobs.claim_job_for_preparation(one, max_attempts=3, lease_seconds=300)
        second = jobs.claim_job_for_preparation(two, max_attempts=3, lease_seconds=300)
    assert first.id == job.id and second is None


async def test_the_worker_renews_the_lease_while_it_prepares(engine, mocker):
    mocker.patch.object(get_settings(), "PREPARE_HEARTBEAT_SECONDS", 0.05)
    release, started = asyncio.Event(), asyncio.Event()

    async def slow_convert(input_path, wav_path):
        started.set()
        await release.wait()
        wav_path.write_bytes(tone(".wav", 1.0))

    mocker.patch.object(prep, "normalize_to_wav", new=slow_convert)
    job = _waiting(engine)
    service = PreparationService()
    await service.poll_once()
    await asyncio.wait_for(started.wait(), 10)
    claimed_at = reload_job(engine, job.id).updated_datetime
    for _ in range(200):  # until the heartbeat has renewed it (a bound, not a wait: it takes a few 50 ms)
        await asyncio.sleep(0.05)
        if reload_job(engine, job.id).updated_datetime > claimed_at:
            break
    assert reload_job(engine, job.id).updated_datetime > claimed_at
    release.set()
    await service.wait_idle()
    assert reload_job(engine, job.id).status == JobStatus.SUBMITTED


# Concurrency and the loop ------------------------------------------------------------------------


async def test_no_more_than_the_configured_number_of_jobs_are_prepared_at_once(engine, mocker):
    mocker.patch.object(get_settings(), "PREPARE_MAX_CONCURRENT", 2)
    release = asyncio.Event()
    running = peak = 0
    two_running = asyncio.Event()

    async def convert(input_path, wav_path):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if running == 2:
            two_running.set()
        await release.wait()
        running -= 1
        wav_path.write_bytes(tone(".wav", 1.0))

    mocker.patch.object(prep, "normalize_to_wav", new=convert)
    waiting = [_waiting(engine) for _ in range(3)]
    service = PreparationService()

    assert await service.poll_once() == 2
    await asyncio.wait_for(two_running.wait(), 20)
    assert peak == 2 and await service.poll_once() == 0  # the third waits for a free place
    release.set()
    await service.wait_idle()
    assert await _prepare_all(service) == 1
    assert peak == 2
    assert {reload_job(engine, j.id).status for j in waiting} == {JobStatus.SUBMITTED}


async def test_jobs_are_taken_oldest_first(engine, mocker):
    mocker.patch.object(get_settings(), "PREPARE_MAX_CONCURRENT", 1)
    newer = _waiting(engine, created_datetime=datetime.now(UTC) - timedelta(minutes=1))
    older = _waiting(engine, created_datetime=datetime.now(UTC) - timedelta(minutes=30))
    service = PreparationService()
    await _prepare_all(service)
    assert reload_job(engine, older.id).status == JobStatus.SUBMITTED
    assert reload_job(engine, newer.id).status == JobStatus.PENDING


async def test_a_job_deleted_while_it_is_being_prepared_is_not_submitted_and_leaves_nothing(engine, no_azure, mocker):
    async def convert_then_the_job_is_deleted(input_path, wav_path):
        wav_path.write_bytes(tone(".wav", 1.0))
        with Session(engine) as session:
            session.delete(session.get(SpeechBatchJob, job.id))
            session.commit()

    mocker.patch.object(prep, "normalize_to_wav", new=convert_then_the_job_is_deleted)
    job = _waiting(engine)

    await _prepare_all(PreparationService())

    assert reload_job(engine, job.id) is None
    assert not _exists(job.audio_blob_path) and not _exists(job.source_blob_path)


async def test_the_polling_loop_prepares_jobs_and_stops_cleanly_when_cancelled(engine, mocker):
    mocker.patch.object(get_settings(), "PREPARE_POLL_INTERVAL_SECONDS", 0.05)
    job = _waiting(engine)
    task = asyncio.create_task(PreparationService().run_polling_loop())
    for _ in range(100):
        await asyncio.sleep(0.1)
        if reload_job(engine, job.id).status == JobStatus.SUBMITTED:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reload_job(engine, job.id).status == JobStatus.SUBMITTED


async def test_a_database_error_in_the_loop_does_not_stop_it(engine, mocker, caplog):
    mocker.patch.object(get_settings(), "PREPARE_POLL_INTERVAL_SECONDS", 0.05)
    calls = 0
    real = PreparationService.poll_once

    async def flaky(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database down")
        return await real(self)

    mocker.patch.object(PreparationService, "poll_once", flaky)
    task = asyncio.create_task(PreparationService().run_polling_loop())
    for _ in range(200):  # until the loop has polled again after the error
        await asyncio.sleep(0.05)
        if calls >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 2


# The claim, directly ------------------------------------------------------------------------------


def _claim(engine, **kwargs):
    with Session(engine) as session:
        job = jobs.claim_job_for_preparation(session, **{"max_attempts": 3, "lease_seconds": 300, **kwargs})
        return None if job is None else (job.id, job.preparation_attempts)


@pytest.mark.parametrize(
    "fields",
    [
        {"source_blob_path": None},  # submitted by its request: nothing to prepare
        {"status": JobStatus.SUBMITTED},
        {"status": JobStatus.FAILED},
        {"batch_job_id": "already-at-azure"},
        {"preparation_attempts": 3},  # used up
    ],
)
async def test_only_a_pending_job_with_a_source_and_attempts_left_is_claimed(engine, fields):
    add_job(engine, **fields)
    assert _claim(engine) is None


async def test_a_claim_counts_the_attempt_and_takes_the_lease(engine):
    job = add_job(engine)
    assert _claim(engine) == (job.id, 1)
    assert _claim(engine) is None  # leased
    assert reload_job(engine, job.id).updated_datetime is not None


async def test_a_lease_that_has_run_out_can_be_taken_over_and_counts_another_attempt(engine):
    job = add_job(engine, preparation_attempts=1, updated_datetime=datetime.now(UTC) - timedelta(minutes=6))
    assert _claim(engine) == (job.id, 2)
    fresh = add_job(engine, preparation_attempts=1, updated_datetime=datetime.now(UTC) - timedelta(minutes=1))
    assert _claim(engine) is None and fresh.id  # still within its lease


async def test_renewing_stamps_the_job_and_reports_a_job_that_is_gone_or_settled(engine):
    job = add_job(engine, preparation_attempts=1, updated_datetime=datetime.now(UTC) - timedelta(minutes=4))
    with Session(engine) as session:
        assert jobs.renew_preparation(session, job.id) is True
        assert jobs.renew_preparation(session, uuid4()) is False
    assert reload_job(engine, job.id).updated_datetime > datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    settled = add_job(engine, status=JobStatus.SUBMITTED)
    with Session(engine) as session:
        assert jobs.renew_preparation(session, settled.id) is False


async def test_a_job_already_submitted_or_deleted_cannot_be_failed_by_a_late_preparation(engine):
    submitted = add_job(engine, status=JobStatus.SUBMITTED, batch_job_id="x")
    with Session(engine) as session:
        assert jobs.mark_preparation_failed(session, submitted.id, "late") is False
        assert jobs.mark_preparation_failed(session, uuid4(), "gone") is False
    assert reload_job(engine, submitted.id).status == JobStatus.SUBMITTED


async def test_the_worker_and_the_upload_request_word_their_refusals_alike():
    """A clerk reads the same sentence whether the file was refused in the request or failed in the worker."""
    from transcribe_api.api import routes_recording

    assert prep.UNDECODABLE_MESSAGE == routes_recording._UNDECODABLE_DETAIL
    assert prep.too_long_detail is routes_recording._too_long_detail
