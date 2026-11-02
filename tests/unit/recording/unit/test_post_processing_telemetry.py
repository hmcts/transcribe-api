"""DIAAT-283: observe post-processing, storage and transcript availability.

Acceptance criteria covered:
* a successful Speech job is told apart from a later post-processing or storage failure
  (results_retrieval, transcript_postprocessing and transcript_persistence are separate
  stages, each with its own controlled code);
* a multi-file stitching failure: not applicable, a job has one audio file and Azure
  returns one transcript file for it (see test_a_job_has_one_transcript_file);
* transcript availability is traceable to the original upload and the job
  (transcript.available carries the job journey, the upload as parent and the Azure
  Speech job ID).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from hmcts_otel import conventions as c
from hmcts_otel import default_schema
from hmcts_otel.testing import TelemetryCapture, capture_telemetry
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.exc import (
    TimeoutError as PoolTimeoutError,
)

from transcribe_api.domain.models_recording import BatchJobStatus, DialogueEntry
from transcribe_api.runtime.telemetry import JOURNEY_JOB, configure_telemetry
from transcribe_api.stt import job_telemetry
from transcribe_api.stt.batch_client import BatchResultError
from transcribe_api.stt.job_telemetry import (
    METRIC_JOB_DURATION,
    PHASE_TO_AVAILABLE,
    db_failure_code,
    failure_code,
    failure_source,
)
from transcribe_api.stt.polling_service import BatchPollingService, _PendingJob
from transcribe_api.stt.speakers_recording import process_speakers

SPEECH_JOB_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SPEECH_URL = f"https://uksouth.api.cognitive.microsoft.com/speechtotext/v3.2/transcriptions/{SPEECH_JOB_ID}"
UPLOAD_ID = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
FILE_STEM = "Smith" + "_v_" + "Home_Office"  # an original file name: never exported
BLOB = f"uploads/00000000-0000-0000-0000-000000000001/{UPLOAD_ID}-{FILE_STEM}.wav"
# Built at runtime so they never appear in a source line the redactor could match.
TRANSCRIPT_TEXT = "the appellant " + "fled " + "in 2019"
SPEAKER_NAME = "Judge " + "Okonkwo"
OTHER_SPEAKER = "Mr " + "Adeyemi"
BASE = "transcribe_api.stt.polling_service"
STAGES = ("results_retrieval", "transcript_postprocessing", "transcript_persistence")


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


def _entry(speaker: str, start: float, text: str = TRANSCRIPT_TEXT) -> DialogueEntry:
    return DialogueEntry(speaker=speaker, text=text, start_time=start, end_time=start + 1.0)


ENTRIES = [
    _entry("1", 0.0),
    _entry("2", 2.0, "No. " + TRANSCRIPT_TEXT),
    _entry("1", 4.0, "Thank you. " + TRANSCRIPT_TEXT),
]


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _pending(**fields) -> _PendingJob:
    created = datetime.now(UTC) - timedelta(hours=1)
    defaults = {
        "audio_blob_path": BLOB,
        "callback_url": None,
        "created_datetime": created,
        "processing_started_datetime": created + timedelta(minutes=5),
    }
    return _PendingJob(
        id=uuid4(),
        batch_job_url=SPEECH_URL,
        batch_job_status=BatchJobStatus.RUNNING,
        caller_id=None,
        metadata_={},
        **{**defaults, **fields},
    )


def _service() -> BatchPollingService:
    service = BatchPollingService(webhook_secret_resolver=lambda _caller: "")
    service._save_results = MagicMock()
    service._record_error = MagicMock()
    service._update_status = MagicMock()
    service._record_cleanup_failure = MagicMock()
    service._dispatch_failure = AsyncMock()
    return service


def _ok() -> dict:
    return _succeeded()


def _succeeded(finished_ago: timedelta | None = timedelta(minutes=10)) -> dict:
    status = {"status": "Succeeded"}
    if finished_ago is not None:
        status["lastActionDateTime"] = _iso(datetime.now(UTC) - finished_ago)
    return status


async def _poll(service, job, status_data=None, *, results=None, speakers=None) -> None:
    with (
        patch(f"{BASE}.get_batch_job_status", new=AsyncMock(return_value=status_data or _ok())),
        patch(f"{BASE}.get_batch_results", new=results or AsyncMock(return_value=list(ENTRIES))),
        patch(f"{BASE}.process_speakers", side_effect=speakers or process_speakers),
        patch(f"{BASE}._resolve_model_display_name", new=AsyncMock(return_value=None)),
        patch(f"{BASE}.delete_batch_job", new=AsyncMock()),
    ):
        await service._process_job(job)


def _journey(tel: TelemetryCapture, job: _PendingJob) -> list[dict]:
    return [e for e in tel.events() if e.get(c.JOURNEY_ID) == str(job.id)]


def _names(events: list[dict]) -> list[str]:
    return [str(e[c.CUSTOM_EVENT_NAME]) for e in events]


def _stage_durations(tel: TelemetryCapture) -> dict[str, dict]:
    return {p["attributes"][c.STAGE]: p["attributes"] for p in tel.metric_points(c.METRIC_STAGE_DURATION)}


# AC1 + AC3: the successful path ------------------------------------------------------------


async def test_success_records_each_step_on_the_job_journey(tel):
    job = _pending()
    await _poll(_service(), job)

    events = _journey(tel, job)
    assert _names(events) == [
        "result_processing.started",
        "results_retrieval.started",
        "results_retrieval.completed",
        "transcript_postprocessing.started",
        "transcript_postprocessing.completed",
        "transcript_persistence.started",
        "transcript_persistence.completed",
        "transcript.available",
        "job.finished",
        "result_processing.completed",
    ]
    for event in events:  # one journey, traceable to the upload and the Azure job
        assert event[c.JOURNEY_TYPE] == JOURNEY_JOB
        assert event[c.JOURNEY_PARENT_ID] == UPLOAD_ID
        assert event[c.DEPENDENCY_REF] == SPEECH_JOB_ID
    for name in STAGES:
        (done,) = tel.events(f"{name}.completed")
        assert done[c.OUTCOME] == "success" and done[c.DURATION_MS] >= 0
    # Metrics are not sampled: each step's duration, for p50/p95 per stage.
    durations = _stage_durations(tel)
    for name in STAGES:
        assert durations[name][c.OUTCOME] == "success"
        assert durations[name][c.JOURNEY_TYPE] == JOURNEY_JOB


async def test_transcript_available_carries_counts_and_time_since_azure_finished(tel):
    job = _pending()
    await _poll(_service(), job, _succeeded(timedelta(minutes=10)))

    (available,) = tel.events("transcript.available")
    assert available[c.JOURNEY_ID] == str(job.id)
    assert available[c.JOURNEY_PARENT_ID] == UPLOAD_ID
    assert available[c.DEPENDENCY_REF] == SPEECH_JOB_ID
    # Three Azure phrases, two speakers, grouped by process_speakers into three turns.
    assert available["transcript.segment_count"] == 3
    assert available["transcript.speaker_count"] == 2
    # Azure finished 10 minutes ago: processing complete -> available.
    assert 600_000 <= available[c.DURATION_MS] < 660_000
    (point,) = [p for p in tel.metric_points(METRIC_JOB_DURATION) if p["attributes"]["phase"] == PHASE_TO_AVAILABLE]
    assert 600 <= point["sum"] < 660


async def test_time_to_available_falls_back_to_the_poll_without_azure_timestamp(tel):
    await _poll(_service(), _pending(), _succeeded(finished_ago=None))
    (available,) = tel.events("transcript.available")
    assert 0 <= available[c.DURATION_MS] < 5_000


def test_transcript_counts_never_raise():
    counts = job_telemetry.transcript_counts
    assert counts([{"speaker": "1"}, {"speaker": "1"}, {"text": "x"}]) == {
        "transcript.segment_count": 3,
        "transcript.speaker_count": 1,
    }
    assert counts(None) == {}  # not iterable: nothing recorded, nothing raised


# AC1: post-processing failure --------------------------------------------------------------


async def test_postprocessing_failure_is_its_own_stage_and_the_job_is_polled_again(tel):
    service = _service()
    job = _pending()
    broken = MagicMock(side_effect=RuntimeError(TRANSCRIPT_TEXT))
    await _poll(service, job, speakers=broken)
    await _poll(service, job, speakers=broken)  # still RUNNING: polled and failed again

    # Azure Speech succeeded and its results were retrieved, both times.
    assert len(tel.events("results_retrieval.completed")) == 2
    assert not tel.events("speech.job_failed")
    failed = tel.events("transcript_postprocessing.failed")
    assert [e[c.ERROR_CODE] for e in failed] == ["transcript.postprocess_failed"] * 2
    assert all(e[c.OUTCOME] == "error" and e[c.DEPENDENCY_REF] == SPEECH_JOB_ID for e in failed)
    umbrella = tel.events("result_processing.failed")
    assert [e[c.ERROR_CODE] for e in umbrella] == ["transcript.postprocess_failed"] * 2
    polls = tel.events("job.poll_failed")
    assert [e[c.ERROR_CODE] for e in polls] == ["transcript.postprocess_failed"] * 2
    assert all(e["hmcts.failure.source"] == "application" for e in polls)
    assert all(e[c.DEPENDENCY_REF] == SPEECH_JOB_ID for e in polls)
    # Behaviour unchanged: nothing saved, the job is not finished either way.
    assert not tel.events("transcript_persistence.started")
    assert not tel.events("transcript.available") and not tel.events("job.finished")
    service._save_results.assert_not_called()
    service._record_error.assert_not_called()
    tel.assert_no_prohibited_content(TRANSCRIPT_TEXT)


async def test_speaker_processing_fallback_is_visible(tel):
    job = _pending()
    with patch(
        "transcribe_api.stt.speakers_recording.group_dialogue_entries_by_speaker",
        side_effect=RuntimeError(TRANSCRIPT_TEXT),
    ):
        await _poll(_service(), job)

    (degraded,) = tel.events("transcript.postprocess_degraded")
    assert degraded["transcript.postprocess_step"] == "speaker_grouping"
    assert degraded[c.JOURNEY_ID] == str(job.id)
    # The transcript is still saved with the ungrouped entries.
    assert tel.events("transcript_postprocessing.completed")
    assert tel.events("transcript.available")[0]["transcript.segment_count"] == 3


# AC1: persistence failure ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (OperationalError("insert", {}, Exception("db down")), "storage.db_unavailable"),
        (IntegrityError("insert", {}, Exception("duplicate")), "storage.db_rejected"),
        (RuntimeError("bug"), "storage.db_failed"),
    ],
    ids=["unavailable", "rejected", "other"],
)
async def test_persistence_failure_is_its_own_stage_with_a_storage_code(tel, exc, code):
    service = _service()
    service._save_results = MagicMock(side_effect=exc)
    job = _pending()
    await _poll(service, job)

    names = _names(_journey(tel, job))
    assert "transcript_postprocessing.completed" in names
    (failed,) = tel.events("transcript_persistence.failed")
    assert failed[c.OUTCOME] == "error" and failed[c.ERROR_CODE] == code
    assert failed[c.DEPENDENCY_REF] == SPEECH_JOB_ID
    assert tel.events("result_processing.failed")[0][c.ERROR_CODE] == code
    (poll,) = tel.events("job.poll_failed")
    assert poll[c.ERROR_CODE] == code and poll["hmcts.failure.source"] == "application"
    assert not tel.events("transcript.available") and not tel.events("job.finished")
    assert _stage_durations(tel)["transcript_persistence"][c.ERROR_CODE] == code
    (storage,) = tel.events("job.storage_failed")
    assert storage["storage.operation"] == "transcript.save"
    assert storage[c.ERROR_CODE] == code and storage["hmcts.failure.source"] == "application"
    assert storage[c.JOURNEY_PARENT_ID] == UPLOAD_ID


async def test_saving_later_makes_the_transcript_available_once(tel):
    service = _service()
    service._save_results = MagicMock(side_effect=[OperationalError("i", {}, Exception()), None])
    job = _pending()
    await _poll(service, job)
    await _poll(service, job)

    assert len(tel.events("transcript_persistence.failed")) == 1
    assert len(tel.events("transcript_persistence.completed")) == 1
    assert len(tel.events("transcript.available")) == 1
    assert len(tel.events("job.finished")) == 1


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (OperationalError("s", {}, Exception()), "storage.db_unavailable"),
        (InterfaceError("s", {}, Exception()), "storage.db_unavailable"),
        (PoolTimeoutError(), "storage.db_unavailable"),
        (IntegrityError("s", {}, Exception()), "storage.db_rejected"),
        (DataError("s", {}, Exception()), "storage.db_rejected"),
        (ProgrammingError("s", {}, Exception()), "storage.db_failed"),
        (ValueError("x"), "storage.db_failed"),
    ],
)
def test_db_failure_code_by_exception_type(exc, code):
    assert db_failure_code(exc) == code


def test_a_tagged_retry_error_keeps_its_code():
    from tenacity import Future, RetryError

    attempt = Future(attempt_number=2)
    attempt.set_exception(OperationalError("s", {}, Exception()))
    exc = RetryError(attempt)
    job_telemetry.tag_failure(exc, "speech.results_error", "dependency")
    assert failure_code(exc, "speech") == "speech.results_error"
    assert failure_source("speech.results_error", exc) == "dependency"


def test_a_tag_on_the_cause_of_a_retry_error_is_read():
    from tenacity import Future, RetryError

    cause = OperationalError("s", {}, Exception())
    job_telemetry.tag_failure(cause, "storage.db_unavailable")
    attempt = Future(attempt_number=2)
    attempt.set_exception(cause)
    assert failure_code(RetryError(attempt), "speech") == "storage.db_unavailable"


def test_a_tagged_exception_keeps_its_step_code():
    exc = ValueError("x")
    assert failure_code(exc, "speech") == "unhandled.ValueError"
    job_telemetry.tag_failure(exc, "storage.db_failed")
    assert failure_code(exc, "speech") == "storage.db_failed"
    job_telemetry.tag_failure(exc, "free text " + TRANSCRIPT_TEXT)  # not a controlled code
    assert failure_code(exc, "speech") == "unhandled.ValueError"


@pytest.mark.parametrize(
    "code",
    [
        "transcript.postprocess_failed",
        "storage.db_unavailable",
        "storage.db_rejected",
        "storage.db_failed",
        "storage.blob_failed",
    ],
)
def test_post_speech_failures_are_the_applications(code):
    assert failure_source(code) == "application"
    # Even when the cause looks like an Azure Speech failure.
    assert failure_source(code, httpx.ConnectError("x")) == "application"


async def test_model_name_resolution_is_not_post_processing(tel):
    # An Azure Speech call (DIAAT-282 measures it), outside our post-processing stage.
    job = _pending()
    with (
        patch(f"{BASE}.process_speakers", side_effect=process_speakers),
        patch(f"{BASE}.get_batch_job_status", new=AsyncMock(return_value=_ok())),
        patch(f"{BASE}.get_batch_results", new=AsyncMock(return_value=list(ENTRIES))),
        patch(
            f"{BASE}._resolve_model_display_name",
            new=AsyncMock(side_effect=RuntimeError("x")),
        ),
    ):
        await _service()._process_job(job)
    assert tel.events("transcript_postprocessing.completed")
    assert not tel.events("transcript_postprocessing.failed")
    assert not tel.events("transcript_persistence.started")


# Results retrieval -------------------------------------------------------------------------


async def test_results_retrieval_failure_is_its_own_stage(tel):
    job = _pending()
    await _poll(_service(), job, results=AsyncMock(side_effect=BatchResultError("x")))

    (failed,) = tel.events("results_retrieval.failed")
    assert failed[c.ERROR_CODE] == "speech.results_unavailable"
    assert failed[c.DEPENDENCY_REF] == SPEECH_JOB_ID
    assert not tel.events("transcript_postprocessing.started")
    finished = tel.events("job.finished")[0]
    assert finished[c.ERROR_CODE] == "speech.results_unavailable"
    assert finished["hmcts.failure.source"] == "dependency"


# Storage writes outside the stages ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "results", "poll_code", "poll_source"),
    [
        # The results stage already attributed the attempt: the poll keeps it.
        (
            _ok(),
            AsyncMock(side_effect=BatchResultError("x")),
            "speech.results_unavailable",
            "dependency",
        ),
        # Azure failed the job; nothing was attributed before the write: the write's code.
        ({"status": "Failed"}, None, "storage.db_unavailable", "application"),
    ],
    ids=["results_unavailable", "azure_failed"],
)
async def test_failing_to_store_a_failure_is_a_storage_failure(tel, status, results, poll_code, poll_source):
    service = _service()
    del service._record_error  # the real write, with the database call failing
    job = _pending()
    with (
        patch(f"{BASE}.Session"),
        patch(f"{BASE}.get_engine"),
        patch(f"{BASE}.mark_job_error", side_effect=OperationalError("u", {}, Exception())),
    ):
        await _poll(service, job, status, results=results)

    (storage,) = tel.events("job.storage_failed")
    assert storage["storage.operation"] == "job.mark_failed"
    assert storage[c.ERROR_CODE] == "storage.db_unavailable"
    assert storage["hmcts.failure.source"] == "application"
    assert storage[c.JOURNEY_ID] == str(job.id)
    (poll,) = tel.events("job.poll_failed")
    assert poll[c.ERROR_CODE] == poll_code and poll["hmcts.failure.source"] == poll_source
    assert not tel.events("job.finished")  # finished only once stored (DIAAT-282)


async def test_audio_blob_not_marked_is_a_blob_failure(tel):
    # set_blob_metadata catches its own errors and returns False.
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=manager)
    manager.__aexit__ = AsyncMock(return_value=False)
    manager.set_blob_metadata = AsyncMock(return_value=False)
    job = _pending()
    with patch(f"{BASE}.AsyncAzureBlobManager", return_value=manager):
        await _poll(_service(), job, {"status": "Failed"})

    (storage,) = tel.events("job.storage_failed")
    assert storage["storage.operation"] == "blob.mark_failed"
    assert storage[c.ERROR_CODE] == "storage.blob_failed"
    assert tel.events("job.finished")[0][c.ERROR_CODE] == "speech.job_failed"


async def test_audio_blob_marked_records_no_failure(tel):
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=manager)
    manager.__aexit__ = AsyncMock(return_value=False)
    manager.set_blob_metadata = AsyncMock(return_value=True)
    with patch(f"{BASE}.AsyncAzureBlobManager", return_value=manager):
        await _poll(_service(), _pending(), {"status": "Failed"})
    assert not tel.events("job.storage_failed")


async def test_failing_to_mark_the_audio_blob_is_a_blob_failure(tel):
    job = _pending()
    status = {"status": "Failed", "properties": {"error": {"message": TRANSCRIPT_TEXT}}}
    with patch(f"{BASE}.AsyncAzureBlobManager", side_effect=RuntimeError(BLOB)):
        await _poll(_service(), job, status)

    (storage,) = tel.events("job.storage_failed")
    assert storage["storage.operation"] == "blob.mark_failed"
    assert storage[c.ERROR_CODE] == "storage.blob_failed"
    assert storage["hmcts.failure.source"] == "application"
    # Behaviour unchanged: the job still finishes as Azure's failure.
    assert tel.events("job.finished")[0][c.ERROR_CODE] == "speech.job_failed"
    assert not tel.events("job.poll_failed")
    tel.assert_no_prohibited_content(BLOB, FILE_STEM, TRANSCRIPT_TEXT)


async def test_failing_to_flag_cleanup_is_a_storage_failure(tel):
    service = _service()
    del service._record_cleanup_failure
    job = _pending()
    with (
        patch(f"{BASE}.Session"),
        patch(f"{BASE}.get_engine"),
        patch(f"{BASE}.mark_needs_cleanup", side_effect=OperationalError("u", {}, Exception())),
        patch(f"{BASE}.get_batch_job_status", new=AsyncMock(return_value=_succeeded())),
        patch(f"{BASE}.get_batch_results", new=AsyncMock(return_value=list(ENTRIES))),
        patch(f"{BASE}._resolve_model_display_name", new=AsyncMock(return_value=None)),
        patch(f"{BASE}.delete_batch_job", new=AsyncMock(side_effect=RuntimeError("x"))),
    ):
        await service._process_job(job)

    (storage,) = tel.events("job.storage_failed")
    assert storage["storage.operation"] == "job.mark_needs_cleanup"
    assert storage[c.ERROR_CODE] == "storage.db_unavailable"


# Delivery to the caller --------------------------------------------------------------------


@pytest.mark.parametrize(("delivered", "event"), [(True, "webhook.delivered"), (False, "webhook.delivery_failed")])
async def test_webhook_outcome_is_recorded_for_the_transcript(tel, delivered, event):
    service = _service()
    service._claim_webhook_dispatch = MagicMock(return_value=True)
    job = _pending(callback_url="https://caller.example/hook", webhook_secret="s")
    with patch(f"{BASE}.dispatch", new=AsyncMock(return_value=delivered)):
        await _poll(service, job)

    (outcome,) = tel.events(event)
    assert outcome["webhook.notification"] == "succeeded"
    assert outcome[c.JOURNEY_ID] == str(job.id)
    names = _names(_journey(tel, job))
    assert names.index("transcript.available") < names.index(event)


async def test_failure_webhook_outcome_says_it_carried_a_failure(tel):
    service = BatchPollingService(webhook_secret_resolver=lambda _caller: "")
    service._claim_webhook_dispatch = MagicMock(return_value=True)
    job = _pending(callback_url="https://caller.example/hook", webhook_secret="s")
    with patch(f"{BASE}.dispatch", new=AsyncMock(return_value=True)):
        await service._dispatch_failure(job, "boom")
    (outcome,) = tel.events("webhook.delivered")
    assert outcome["webhook.notification"] == "failed"


# Privacy -----------------------------------------------------------------------------------


async def test_no_transcript_text_speaker_names_or_blob_path_is_exported(tel):
    entries = [
        _entry(SPEAKER_NAME, 0.0),
        _entry(OTHER_SPEAKER, 2.0, "No. " + TRANSCRIPT_TEXT),
    ]
    service = _service()
    service._claim_webhook_dispatch = MagicMock(return_value=True)
    ok = _pending(callback_url="https://caller.example/hook", webhook_secret="s")
    with patch(f"{BASE}.dispatch", new=AsyncMock(return_value=True)):
        # Speaker names kept as they are (no relabelling), so they reach every step.
        await _poll(service, ok, results=AsyncMock(return_value=entries), speakers=lambda e: e)
    # Failures whose exception messages carry the content.
    await _poll(
        service,
        _pending(),
        speakers=MagicMock(side_effect=ValueError(f"{SPEAKER_NAME}: {TRANSCRIPT_TEXT} {BLOB}")),
    )
    db_error = OperationalError(BLOB, {}, Exception(SPEAKER_NAME))
    service._save_results = MagicMock(side_effect=db_error)
    await _poll(service, _pending(), results=AsyncMock(return_value=entries))

    assert tel.events("transcript.available")[0]["transcript.speaker_count"] == 2
    assert tel.events("transcript_postprocessing.failed")
    assert tel.events("transcript_persistence.failed")
    tel.assert_no_prohibited_content(TRANSCRIPT_TEXT, SPEAKER_NAME, OTHER_SPEAKER, BLOB, FILE_STEM)


# Schema and stitching ----------------------------------------------------------------------


def test_new_events_are_registered(tel):
    for name in (
        "transcript.available",
        "transcript.postprocess_degraded",
        "job.storage_failed",
        "webhook.delivered",
        *(f"{stage}.{verb}" for stage in STAGES for verb in ("started", "completed", "failed")),
    ):
        assert default_schema.get_event(name) is not None, name
    available = default_schema.get_event("transcript.available")
    assert {"transcript.segment_count", "transcript.speaker_count"} <= set(available.attributes)


def test_a_job_has_one_transcript_file():
    """AC2 (stitching) is not applicable: no job combines files.

    A job is submitted with one audio URL, and only the first Transcription file Azure
    returns is read. Multi-file hearings are several uploads (one job each) sharing a
    hearing ID (DIAAT-280), not one stitched transcript.
    """
    import inspect

    from transcribe_api.stt import batch_client

    assert '"contentUrls": [audio_sas_url]' in inspect.getsource(batch_client.submit_batch_job)
    assert "transcription_files[0]" in inspect.getsource(batch_client.get_batch_results)
