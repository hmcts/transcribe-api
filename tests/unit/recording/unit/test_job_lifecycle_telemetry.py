"""DIAAT-281: observe the transcription-job lifecycle and its database-driven "queue".

Acceptance criteria covered:
1. the current number of waiting and running jobs, and the oldest waiting job, are
   visible (gauges computed from the transcription_job table);
2. a stuck or failed job is identifiable and traceable (job.stuck once per job, on the
   job journey; every terminal failure ends in job.finished with a controlled code;
   retries and failed polls are recorded);
3. queue time is separated from transcription-processing time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from hmcts_otel import conventions as c
from hmcts_otel import get_meter
from hmcts_otel.testing import TelemetryCapture, capture_telemetry
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from transcribe_api.stt import job_telemetry
from transcribe_api.stt.job_telemetry import (
    METRIC_JOB_DURATION,
    METRIC_JOB_RETRIES,
    METRIC_JOBS_FINISHED,
    METRIC_JOBS_RUNNING,
    METRIC_JOBS_STUCK,
    METRIC_JOBS_WAITING,
    METRIC_OLDEST_WAITING_AGE,
    METRIC_QUEUE_DEPTH,
    JobStateGauges,
)
from transcribe_api.stt.polling_service import BatchPollingService, _PendingJob
from transcribe_api.domain.interface_recording import count_job_states, update_job_batch_status
from transcribe_api.domain.models_recording import BatchJobStatus, JobStatus, SpeechBatchJob
from transcribe_api.runtime.telemetry import JOURNEY_JOB, configure_telemetry

SPEECH_JOB_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SPEECH_URL = f"https://uksouth.api.cognitive.microsoft.com/speechtotext/v3.2/transcriptions/{SPEECH_JOB_ID}"
UPLOAD_ID = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
FILE_STEM = "Smith" + "_v_" + "Home_Office"  # an original file name: never exported
BLOB = f"uploads/00000000-0000-0000-0000-000000000001/{UPLOAD_ID}-{FILE_STEM}.wav"
BASE = "transcribe_api.stt.polling_service"


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):  # the unit suite has no Postgres
    return "JSON"


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _add(engine, status: JobStatus, age: timedelta, **fields) -> SpeechBatchJob:
    job = SpeechBatchJob(
        audio_url="https://x/y.wav",
        status=status,
        created_datetime=datetime.now(UTC) - age,
        audio_blob_path=BLOB,
        **fields,
    )
    with Session(engine) as session:
        session.add(job)
        session.commit()
        session.refresh(job)
    return job


def _value(tel: TelemetryCapture, metric: str, **attributes) -> float:
    (point,) = [p for p in tel.metric_points(metric) if p["attributes"] == attributes]
    return point["value"]


def _for_job(tel: TelemetryCapture, job_id) -> list[str]:
    return [str(e[c.CUSTOM_EVENT_NAME]) for e in tel.events() if e.get(c.JOURNEY_ID) == str(job_id)]


# AC1: waiting, running and oldest waiting job -------------------------------------------


def _queue(engine) -> None:
    _add(engine, JobStatus.PENDING, timedelta(hours=2))  # waiting, past the 30 min threshold
    _add(engine, JobStatus.SUBMITTED, timedelta(minutes=5))  # waiting in Azure's queue
    _add(engine, JobStatus.RUNNING, timedelta(hours=1))
    _add(engine, JobStatus.RUNNING, timedelta(hours=9))  # running, past the 3 h threshold
    _add(engine, JobStatus.SUCCEEDED, timedelta(days=3))  # finished jobs are not counted
    _add(engine, JobStatus.FAILED, timedelta(days=3))


def test_gauges_show_waiting_running_and_oldest_waiting_job(tel, engine):
    _queue(engine)
    JobStateGauges(lambda: Session(engine)).register(get_meter())

    assert _value(tel, METRIC_JOBS_WAITING) == 2
    assert _value(tel, METRIC_JOBS_RUNNING) == 2
    assert _value(tel, METRIC_QUEUE_DEPTH) == 4
    assert 2 * 3600 <= _value(tel, METRIC_OLDEST_WAITING_AGE) < 2 * 3600 + 60
    assert _value(tel, METRIC_JOBS_STUCK, **{"job.state": "waiting"}) == 1
    assert _value(tel, METRIC_JOBS_STUCK, **{"job.state": "running"}) == 1


def test_oldest_waiting_age_is_zero_with_nothing_waiting(tel, engine):
    _add(engine, JobStatus.RUNNING, timedelta(hours=1))
    JobStateGauges(lambda: Session(engine)).register(get_meter())
    assert _value(tel, METRIC_OLDEST_WAITING_AGE) == 0
    assert _value(tel, METRIC_JOBS_WAITING) == 0


def test_job_reported_stuck_leaves_oldest_waiting_age_but_stays_stuck(tel, engine):
    # A PENDING orphan (no batch job, never polled) that has already been reported stuck.
    _add(engine, JobStatus.PENDING, timedelta(days=2), stuck_reported_at=datetime.now(UTC))
    _add(engine, JobStatus.SUBMITTED, timedelta(minutes=5))
    JobStateGauges(lambda: Session(engine)).register(get_meter())

    assert 5 * 60 <= _value(tel, METRIC_OLDEST_WAITING_AGE) < 6 * 60
    assert _value(tel, METRIC_JOBS_STUCK, **{"job.state": "waiting"}) == 1
    assert _value(tel, METRIC_JOBS_WAITING) == 2


def test_old_waiting_job_not_yet_reported_stuck_counts_towards_oldest_waiting_age(engine):
    now = datetime.now(UTC)
    _add(engine, JobStatus.PENDING, timedelta(hours=2))  # past the threshold, not yet reported
    _add(engine, JobStatus.SUBMITTED, timedelta(days=1), stuck_reported_at=now)
    with Session(engine) as session:
        counts = count_job_states(
            session,
            waiting_cutoff=now - timedelta(minutes=30),
            running_cutoff=now - timedelta(hours=3),
        )
    assert counts.oldest_waiting_created is not None
    age = now - counts.oldest_waiting_created.replace(tzinfo=UTC)
    assert timedelta(hours=2, minutes=-1) < age < timedelta(hours=2, minutes=1)
    assert counts.waiting == 2 and counts.stuck_waiting == 2


def test_running_jobs_are_stuck_by_time_since_processing_started(engine):
    now = datetime.now(UTC)
    # Created long ago, but Azure only started it 10 minutes ago: not stuck.
    _add(
        engine,
        JobStatus.RUNNING,
        timedelta(hours=9),
        processing_started_datetime=now - timedelta(minutes=10),
    )
    with Session(engine) as session:
        counts = count_job_states(
            session,
            waiting_cutoff=now - timedelta(minutes=30),
            running_cutoff=now - timedelta(hours=3),
        )
    assert counts.running == 1 and counts.stuck_running == 0


def test_gauges_share_one_query_per_export_interval(tel):
    factory = MagicMock(side_effect=lambda: _FakeSession(waiting=3))
    clock = MagicMock(return_value=100.0)
    JobStateGauges(factory, max_age_s=15, clock=clock).register(get_meter())

    tel.metric_points(METRIC_JOBS_WAITING)  # one collection observes all five gauges
    assert factory.call_count == 1
    clock.return_value = 110.0
    tel.metric_points(METRIC_JOBS_WAITING)
    assert factory.call_count == 1  # still fresh
    clock.return_value = 116.0
    assert _value(tel, METRIC_JOBS_WAITING) == 3
    assert factory.call_count == 2


def test_database_error_skips_the_gauges_without_failing_the_export(tel, caplog):
    factory = MagicMock(side_effect=ConnectionError("db down"))
    JobStateGauges(factory, clock=lambda: 0.0).register(get_meter())

    with caplog.at_level(logging.WARNING):
        assert tel.metric_points(METRIC_JOBS_WAITING) == []
        assert tel.metric_points(METRIC_OLDEST_WAITING_AGE) == []
    assert factory.call_count == 1  # the failure is cached too, not retried per gauge
    assert "Job state gauges skipped" in caplog.text
    assert "db down" not in caplog.text


def test_gauges_are_registered_once_per_meter_provider(tel):
    factory = MagicMock(side_effect=lambda: _FakeSession(waiting=1))
    assert job_telemetry.register_job_gauges(factory) is True
    assert job_telemetry.register_job_gauges(factory) is False
    assert _value(tel, METRIC_JOBS_WAITING) == 1


async def test_the_pollers_start_with_the_gauges():
    """CNP: the pollers run in the worker (or, optionally, the web process); the
    process that polls also reports the queue."""
    from transcribe_api.api.app import start_background_workers, stop_background_workers
    from transcribe_api.stt.work_poller import TranscriptionPollingService

    with (
        patch.object(job_telemetry, "register_job_gauges") as register,
        patch.object(BatchPollingService, "run_polling_loop", new=AsyncMock()),
        patch.object(TranscriptionPollingService, "__init__", return_value=None),
        patch.object(TranscriptionPollingService, "run_polling_loop", new=AsyncMock()),
    ):
        tasks = await start_background_workers()
        await stop_background_workers(tasks)
    register.assert_called_once_with()


class _FakeSession:
    def __init__(self, waiting: int) -> None:
        self.waiting = waiting

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _stmt):
        result = MagicMock()
        result.one.return_value = (self.waiting, 0, None, 0, 0)
        return result


# AC2: stuck jobs ------------------------------------------------------------------------


def test_stuck_job_is_reported_once_on_its_journey(tel, engine):
    waiting = _add(engine, JobStatus.SUBMITTED, timedelta(hours=1), batch_job_id=SPEECH_JOB_ID)
    started = datetime.now(UTC) - timedelta(hours=4)
    running = _add(engine, JobStatus.RUNNING, timedelta(hours=5), processing_started_datetime=started)
    _add(engine, JobStatus.PENDING, timedelta(minutes=1))  # not stuck yet

    with Session(engine) as session:
        assert job_telemetry.report_stuck_jobs(session) == 2
    with Session(engine) as session:
        assert job_telemetry.report_stuck_jobs(session) == 0  # next poll: nothing new

    events = {e[c.JOURNEY_ID]: e for e in tel.events("job.stuck")}
    assert set(events) == {str(waiting.id), str(running.id)}
    w = events[str(waiting.id)]
    assert w[c.JOURNEY_TYPE] == JOURNEY_JOB and w[c.JOURNEY_PARENT_ID] == UPLOAD_ID
    assert w["job.state"] == "waiting" and w[c.DEPENDENCY_REF] == SPEECH_JOB_ID
    assert 3600_000 <= w[c.DURATION_MS] < 3660_000
    r = events[str(running.id)]
    assert r["job.state"] == "running"
    assert 4 * 3600_000 <= r[c.DURATION_MS] < 4 * 3600_000 + 60_000  # since processing began
    tel.assert_no_prohibited_content(FILE_STEM)


def test_stuck_threshold_is_configurable(tel, engine, monkeypatch):
    from transcribe_api.runtime.settings_recording import get_settings

    _add(engine, JobStatus.PENDING, timedelta(minutes=10))
    monkeypatch.setenv("JOB_STUCK_WAITING_SECONDS", "300")
    get_settings.cache_clear()
    with Session(engine) as session:
        assert job_telemetry.report_stuck_jobs(session) == 1


def test_stuck_thresholds_must_be_positive(monkeypatch):
    from pydantic import ValidationError

    from transcribe_api.runtime.settings_recording import Settings

    with pytest.raises(ValidationError):
        Settings(JOB_STUCK_RUNNING_SECONDS=0)


async def test_polling_loop_checks_for_stuck_jobs_and_survives_errors(monkeypatch):
    service = BatchPollingService(webhook_secret_resolver=lambda _caller: "")
    service._poll_once = AsyncMock()
    service._claim_and_report_stuck_jobs = MagicMock(side_effect=ConnectionError("db down"))

    async def stop(_seconds):
        service._shutdown = True

    monkeypatch.setattr(asyncio, "sleep", stop)
    await service.run_polling_loop()
    service._claim_and_report_stuck_jobs.assert_called_once()


# AC3: queue time separated from processing time -----------------------------------------


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _pending(created: datetime, status: BatchJobStatus, started: datetime | None = None):
    return _PendingJob(
        id=uuid4(),
        batch_job_url=SPEECH_URL,
        batch_job_status=status,
        callback_url=None,
        caller_id=None,
        metadata_={},
        audio_blob_path=BLOB,
        created_datetime=created,
        processing_started_datetime=started,
    )


def _service() -> BatchPollingService:
    service = BatchPollingService(webhook_secret_resolver=lambda _caller: "")
    service._save_results = MagicMock()
    service._record_error = MagicMock()
    service._update_status = MagicMock()
    service._record_cleanup_failure = MagicMock()
    return service


async def _poll(service, job, status_data) -> None:
    with (
        patch(f"{BASE}.get_batch_job_status", new=AsyncMock(return_value=status_data)),
        patch(f"{BASE}.get_batch_results", new=AsyncMock(return_value=[])),
        patch(f"{BASE}.process_speakers", side_effect=lambda entries: entries),
        patch(f"{BASE}._resolve_model_display_name", new=AsyncMock(return_value=None)),
        patch(f"{BASE}.delete_batch_job", new=AsyncMock()),
    ):
        await service._process_job(job)


def _durations(tel: TelemetryCapture) -> dict[str, float]:
    return {p["attributes"]["phase"]: p["sum"] for p in tel.metric_points(METRIC_JOB_DURATION)}


async def test_queue_wait_and_processing_are_measured_separately(tel):
    now = datetime.now(UTC)
    created = now - timedelta(hours=1)
    azure_started = created + timedelta(minutes=10)
    azure_finished = created + timedelta(minutes=40)
    service = _service()

    # First poll that sees Running: processing starts at Azure's lastActionDateTime.
    waiting = _pending(created, BatchJobStatus.NOT_STARTED)
    await _poll(service, waiting, {"status": "Running", "lastActionDateTime": _iso(azure_started)})
    service._update_status.assert_called_once_with(waiting.id, BatchJobStatus.RUNNING, azure_started)
    (running,) = tel.events("speech.job_running")
    assert running[c.DURATION_MS] == pytest.approx(600_000)  # the queue wait
    assert running[c.DEPENDENCY_REF] == SPEECH_JOB_ID

    # A later poll sees Succeeded.
    done = _pending(created, BatchJobStatus.RUNNING, started=azure_started)
    await _poll(service, done, {"status": "Succeeded", "lastActionDateTime": _iso(azure_finished)})
    (finished,) = tel.events("job.finished")
    assert finished[c.OUTCOME] == "success"
    assert finished["job.queue_wait_ms"] == pytest.approx(600_000)
    assert finished["job.processing_ms"] == pytest.approx(1_800_000)
    assert 3600_000 <= finished[c.DURATION_MS] < 3660_000  # end to end: created -> available

    durations = _durations(tel)
    assert durations["queue_wait"] == pytest.approx(600)
    assert durations["processing"] == pytest.approx(1800)
    assert 3600 <= durations["end_to_end"] < 3660
    tel.assert_no_prohibited_content(FILE_STEM)


async def test_unknown_processing_start_is_not_guessed(tel):
    # Azure went from NotStarted to Succeeded between two polls.
    job = _pending(datetime.now(UTC) - timedelta(minutes=20), BatchJobStatus.NOT_STARTED)
    await _poll(_service(), job, {"status": "Succeeded"})
    (finished,) = tel.events("job.finished")
    assert "job.queue_wait_ms" not in finished and "job.processing_ms" not in finished
    assert 1200_000 <= finished[c.DURATION_MS] < 1260_000
    assert set(_durations(tel)) == {"end_to_end", "to_available"}  # no processing phase


def test_processing_start_is_kept_between_creation_and_now():
    now = datetime.now(UTC)
    created = now - timedelta(minutes=5)
    start = job_telemetry.processing_started_at
    assert start({"lastActionDateTime": _iso(now - timedelta(minutes=1))}, created, now) == (now - timedelta(minutes=1))
    assert start({"lastActionDateTime": _iso(now - timedelta(hours=1))}, created, now) == created
    assert start({"lastActionDateTime": _iso(now + timedelta(hours=1))}, created, now) == now
    assert start({"lastActionDateTime": "garbage"}, created, now) == now
    assert start({}, created, now) == now


def test_processing_start_is_recorded_once(engine):
    job = _add(engine, JobStatus.SUBMITTED, timedelta(minutes=5))
    first = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    with Session(engine) as session:
        update_job_batch_status(session, job.id, BatchJobStatus.RUNNING, first)
        update_job_batch_status(session, job.id, BatchJobStatus.RUNNING, first + timedelta(1))
    with Session(engine) as session:
        stored = session.get(SpeechBatchJob, job.id)
        assert stored.status == JobStatus.RUNNING
        assert stored.processing_started_datetime.replace(tzinfo=UTC) == first


async def test_job_is_queued_when_azure_accepts_it(tel, mocker):
    from transcribe_api.stt import submission

    mocker.patch.object(submission, "save_job", side_effect=lambda _session, job: job)
    mocker.patch.object(submission, "submit_batch_job", new=AsyncMock(return_value=SPEECH_URL))
    job = await submission.submit_and_queue_batch_job(
        session=MagicMock(), audio_url="https://x/y.wav", audio_blob_path=BLOB
    )
    assert _for_job(tel, job.id)[-1] == "job.queued"
    (queued,) = tel.events("job.queued")
    assert queued[c.DEPENDENCY_REF] == SPEECH_JOB_ID and queued[c.DURATION_MS] >= 0


# AC2: failures and retries --------------------------------------------------------------


async def test_failed_job_is_terminal_with_a_controlled_code_and_counted(tel):
    service = _service()
    created = datetime.now(UTC) - timedelta(minutes=30)
    job = _pending(created, BatchJobStatus.RUNNING, started=created + timedelta(minutes=5))
    with patch(f"{BASE}.AsyncAzureBlobManager"):
        await _poll(
            service,
            job,
            {
                "status": "Failed",
                "lastActionDateTime": _iso(created + timedelta(minutes=25)),
                "properties": {"error": {"message": "could not decode " + FILE_STEM}},
            },
        )
    (finished,) = tel.events("job.finished")
    assert finished[c.OUTCOME] == "error" and finished[c.ERROR_CODE] == "speech.job_failed"
    assert finished["job.processing_ms"] == pytest.approx(1_200_000)
    (counted,) = tel.metric_points(METRIC_JOBS_FINISHED)
    assert counted["attributes"] == {
        c.OUTCOME: "error",
        c.ERROR_CODE: "speech.job_failed",
        "hmcts.failure.source": "dependency",
    }
    assert counted["value"] == 1
    tel.assert_no_prohibited_content(FILE_STEM)


async def test_results_failure_ends_the_job_with_its_code(tel):
    from transcribe_api.stt.batch_client import BatchResultError

    service = _service()
    service._dispatch_failure = AsyncMock()
    job = _pending(datetime.now(UTC), BatchJobStatus.RUNNING)
    with (
        patch(f"{BASE}.get_batch_job_status", new=AsyncMock(return_value={"status": "Succeeded"})),
        patch(f"{BASE}.get_batch_results", new=AsyncMock(side_effect=BatchResultError("x"))),
    ):
        await service._process_job(job)
    (counted,) = tel.metric_points(METRIC_JOBS_FINISHED)
    assert counted["attributes"][c.ERROR_CODE] == "speech.results_unavailable"


async def test_submission_failure_is_counted_as_finished(tel, mocker):
    from transcribe_api.stt import submission
    from transcribe_api.stt.batch_client import BatchSubmissionError

    mocker.patch.object(submission, "save_job", side_effect=lambda _session, job: job)
    mocker.patch.object(submission, "submit_batch_job", new=AsyncMock(side_effect=BatchSubmissionError("no")))
    with pytest.raises(BatchSubmissionError):
        await submission.submit_and_queue_batch_job(session=MagicMock(), audio_url="https://x")
    (finished,) = tel.events("job.finished")
    assert c.DURATION_MS in finished  # end to end, created -> failed
    (counted,) = tel.metric_points(METRIC_JOBS_FINISHED)
    assert counted["attributes"][c.ERROR_CODE] == "speech.submission_rejected"


async def test_failed_poll_leaves_a_trace_with_a_controlled_code(tel):
    job = _pending(datetime.now(UTC), BatchJobStatus.RUNNING)
    error = httpx.ConnectError("connect to " + FILE_STEM)
    with patch(f"{BASE}.get_batch_job_status", new=AsyncMock(side_effect=error)):
        await _service()._process_job(job)
    (failed,) = tel.events("job.poll_failed")
    assert failed[c.JOURNEY_ID] == str(job.id)
    assert failed[c.ERROR_CODE] == "speech.connect_error"
    assert not tel.events("job.finished")  # not terminal: polled again next interval
    tel.assert_no_prohibited_content(FILE_STEM)


def test_failure_code_unwraps_exhausted_retries():
    from tenacity import Future, RetryError

    response = httpx.Response(503, request=httpx.Request("GET", SPEECH_URL))
    attempt = Future(attempt_number=2)
    attempt.set_exception(httpx.HTTPStatusError("x", request=response.request, response=response))
    assert job_telemetry.failure_code(RetryError(attempt), "speech") == "speech.http_503"
    assert job_telemetry.failure_code(ValueError("x"), "speech") == "unhandled.ValueError"
    assert job_telemetry.failure_code(httpx.ReadTimeout("x"), "webhook") == "webhook.timeout"


async def test_speech_call_retry_is_recorded_with_its_attempt(tel, monkeypatch):
    from hmcts_otel import journey

    from transcribe_api.stt.batch_client import get_batch_job_status

    request = httpx.Request("GET", SPEECH_URL)
    responses = [
        httpx.Response(503, request=request),
        httpx.Response(200, json={}, request=request),
    ]

    async def get(self, url, **_kwargs):
        return responses.pop(0)

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    job_id = str(uuid4())
    with journey(JOURNEY_JOB, job_id):
        assert await get_batch_job_status(SPEECH_URL) == {}

    (retried,) = tel.events("job.retried")
    assert retried[c.JOURNEY_ID] == job_id
    assert retried["job.retry.operation"] == "speech.status"
    assert retried["job.retry.attempt"] == 1
    assert retried[c.ERROR_CODE] == "speech.http_503"
    (counted,) = tel.metric_points(METRIC_JOB_RETRIES)
    assert counted["value"] == 1


async def test_webhook_retries_are_recorded_and_final_failure_is_visible(tel, monkeypatch):
    from transcribe_api.domain.webhook import dispatch

    async def post(self, url, **_kwargs):
        return httpx.Response(502, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    assert await dispatch("https://caller.example/hook", "secret", {}, max_retries=3) is False
    attempts = [e["job.retry.attempt"] for e in tel.events("job.retried")]
    assert attempts == [1, 2]  # the third attempt is the last: no retry after it
    assert {e["job.retry.operation"] for e in tel.events("job.retried")} == {"webhook.deliver"}
    assert {e[c.ERROR_CODE] for e in tel.events("job.retried")} == {"webhook.http_502"}

    service = _service()
    service._claim_webhook_dispatch = MagicMock(return_value=True)
    job = _PendingJob(
        id=uuid4(),
        batch_job_url=SPEECH_URL,
        batch_job_status=BatchJobStatus.RUNNING,
        callback_url="https://caller.example/hook",
        caller_id=uuid4(),
        metadata_={},
        webhook_secret="secret",
    )
    with patch(f"{BASE}.dispatch", new=AsyncMock(return_value=False)):
        await service._dispatch_failure(job, "boom")
        await service._dispatch_success(job, [])
    codes = [e[c.ERROR_CODE] for e in tel.events("webhook.delivery_failed")]
    assert codes == ["webhook.not_delivered", "webhook.not_delivered"]


def test_lifecycle_events_are_registered_in_the_schema(tel):
    from hmcts_otel import default_schema

    for name in (
        "job.created",
        "job.queued",
        "speech.job_running",
        "speech.job_failed",
        "transcript.available",
        "job.finished",
        "job.retried",
        "job.poll_failed",
        "job.stuck",
        "webhook.delivery_failed",
    ):
        assert default_schema.get_event(name) is not None, name
