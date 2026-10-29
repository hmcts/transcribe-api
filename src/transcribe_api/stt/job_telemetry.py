"""Transcription-job lifecycle telemetry (DIAAT-281).

Builds on the job journey of DIAAT-276 (``recording.job``, id = job id, parent = the
upload id) and adds what makes waiting, stuck, retried and failed jobs visible.

Timings, from timestamps the service already has (plus processing_started_datetime):

* queue wait: job created (upload complete, row inserted) -> Azure Speech started
  processing it (its ``lastActionDateTime`` on the first poll that sees Running). It
  includes our submission call (also the ``speech_submission`` stage duration, and
  ``job.queued``'s duration_ms) and the time Azure held the job as NotStarted.
* processing: Azure processing started -> Azure finished (its ``lastActionDateTime``
  when it reports Succeeded or Failed; the poll's time if missing).
* end to end: job created -> transcript available (or the job's terminal failure).
  What remains after queue wait and processing is our poll lag and result handling.

There is no message queue: work is driven by polling the transcription_job table, so
the queue gauges are computed from that table (see ``JobStateGauges``).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from hmcts_otel import SpeechMode, StageHandle, get_meter, journey, record_event
from hmcts_otel import conventions as c
from hmcts_otel import record_speech_usage as _record_speech_usage
from opentelemetry.metrics import CallbackOptions, Meter, Observation
from sqlalchemy import exc as sa_exc
from sqlmodel import Session

from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.domain.interface_recording import (
    JobStateCounts,
    claim_stuck_jobs,
    count_job_states,
)
from transcribe_api.domain.models_recording import JobStatus
from transcribe_api.runtime.telemetry import (
    AI_FEATURE_BATCH_TRANSCRIPTION,
    ATTR_AUDIO_DURATION_S,
    ATTR_AUDIO_DURATION_SOURCE,
    ATTR_FAILURE_SOURCE,
    ATTR_HTTP_STATUS,
    ATTR_JOB_STATE,
    ATTR_PROCESSING_MS,
    ATTR_QUEUE_WAIT_MS,
    ATTR_RETRY_AFTER_S,
    ATTR_RETRY_ATTEMPT,
    ATTR_RETRY_OPERATION,
    ATTR_SEGMENT_COUNT,
    ATTR_SPEAKER_COUNT,
    ATTR_SPEECH_ERROR_CODE,
    ATTR_STORAGE_OPERATION,
    EVENT_JOB_FINISHED,
    EVENT_JOB_RETRIED,
    EVENT_JOB_RUNNING,
    EVENT_JOB_STUCK,
    EVENT_STORAGE_FAILED,
    EVENT_TRANSCRIPT_AVAILABLE,
    JOURNEY_JOB,
    batch_job_id_from_url,
    upload_id_from_blob,
)

logger = logging.getLogger(__name__)

METRIC_JOB_DURATION = "transcription.job.duration"
METRIC_JOBS_FINISHED = "transcription.jobs.finished"
METRIC_JOB_RETRIES = "transcription.job.retries"
METRIC_JOBS_WAITING = "transcription.jobs.waiting"
METRIC_JOBS_RUNNING = "transcription.jobs.running"
METRIC_QUEUE_DEPTH = "transcription.jobs.queue_depth"
METRIC_OLDEST_WAITING_AGE = "transcription.jobs.oldest_waiting_age"
METRIC_JOBS_STUCK = "transcription.jobs.stuck"
# DIAAT-277: seconds since the polling loop last completed a poll (its heartbeat).
METRIC_POLLER_LAST_SUCCESS_AGE = "transcription.poller.last_success_age"
# DIAAT-282: running jobs / SPEECH_CONCURRENT_JOBS_LIMIT, only when the limit is set.
METRIC_SPEECH_UTILISATION = "transcription.speech.concurrency.utilisation"

# OBS-5: jobs whose Speech usage was recorded, by speech.audio_duration_source (azure,
# estimate, or none when nothing was recorded).
METRIC_SPEECH_USAGE_JOBS = "transcription.speech.usage.jobs"
DURATION_SOURCE_AZURE = "azure"
DURATION_SOURCE_ESTIMATE = "estimate"
DURATION_SOURCE_NONE = "none"
# The submitted estimate is the caller's: one beyond a day is not a recording's length,
# and recording it would skew the usage share Speech cost is allocated by.
MAX_ESTIMATED_AUDIO_SECONDS = 24 * 60 * 60

# Values of the "phase" attribute on transcription.job.duration.
PHASE_QUEUE_WAIT = "queue_wait"
PHASE_PROCESSING = "processing"
PHASE_END_TO_END = "end_to_end"
# DIAAT-283: Azure Speech finished -> transcript saved and available. The duration of
# each post-Speech step is on hmcts.stage.duration (stage = results_retrieval,
# transcript_postprocessing, transcript_persistence), which every stage() records.
PHASE_TO_AVAILABLE = "to_available"


# Timestamps -----------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def seconds_between(start: datetime | None, end: datetime | None) -> float | None:
    """Seconds from start to end, never negative (clock skew); None if either is unknown."""
    if start is None or end is None:
        return None
    return max(0.0, (_utc(end) - _utc(start)).total_seconds())


def azure_last_action(status_data: Mapping[str, Any]) -> datetime | None:
    """Azure's ``lastActionDateTime``: when the job entered its current status."""
    value = status_data.get("lastActionDateTime")
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def processing_started_at(
    status_data: Mapping[str, Any], created_at: datetime | None, now: datetime | None = None
) -> datetime:
    """When Azure started processing, from the first status response that says Running.

    Azure's own timestamp, kept within [job created, now]; the poll's time without it.
    """
    now = now or datetime.now(UTC)
    started = azure_last_action(status_data)
    if started is None or started > now:
        return now
    if created_at is not None and started < _utc(created_at):
        return _utc(created_at)
    return started


# Instruments ----------------------------------------------------------------------------

_instruments: dict[str, tuple[Meter, Any]] = {}
_instruments_lock = threading.Lock()


def _instrument(kind: str, name: str, **kwargs: Any) -> Any:
    """One instrument per name for the current meter provider (tests replace it)."""
    meter = get_meter()
    with _instruments_lock:
        existing = _instruments.get(name)
        if existing is not None and existing[0] is meter:
            return existing[1]
        factory = {"histogram": meter.create_histogram, "counter": meter.create_counter}[kind]
        created = factory(name, **kwargs)
        _instruments[name] = (meter, created)
        return created


def _record_duration(phase: str, seconds: float | None, outcome: str | None = None) -> None:
    if seconds is None:
        return
    attributes = {"phase": phase}
    if outcome is not None:
        attributes[c.OUTCOME] = outcome
    try:
        _instrument(
            "histogram",
            METRIC_JOB_DURATION,
            unit="s",
            description="Transcription job time by phase (queue wait, processing, end to end...)",
        ).record(seconds, attributes)
    except Exception as exc:  # telemetry must never break the job
        logger.warning("Could not record job duration (%s)", type(exc).__name__)


# Lifecycle events -----------------------------------------------------------------------


def record_processing_started(
    attributes: Mapping[str, object], created_at: datetime | None, started_at: datetime
) -> None:
    """speech.job_running, with the queue wait as its duration."""
    queue_wait = seconds_between(created_at, started_at)
    record_event(
        EVENT_JOB_RUNNING,
        attributes,
        duration_ms=queue_wait * 1000.0 if queue_wait is not None else None,
    )
    _record_duration(PHASE_QUEUE_WAIT, queue_wait)


def record_job_finished(
    outcome: str,
    error_code: str | None = None,
    *,
    created_at: datetime | None,
    processing_started: datetime | None = None,
    processing_ended: datetime | None = None,
    exc: BaseException | None = None,
    attributes: Mapping[str, object] | None = None,
) -> None:
    """The job's terminal state: job.finished, its durations and the finished-jobs count.

    duration_ms is the end-to-end time. Queue wait and processing are attached when the
    processing start is known (not for jobs that never reached Azure, or that Azure
    finished between two polls).

    DIAAT-282: a failed job also carries hmcts.failure.source (from the code and, when
    given, the exception that caused it) and what Azure said about the failure.
    ``attributes`` adds the Azure Speech job ID and configuration reference.
    """
    now = datetime.now(UTC)
    source = "none"
    event: dict[str, object] = {**(attributes or {}), c.OUTCOME: outcome}
    if outcome != "success":
        failure = failure_attributes(error_code or "unknown", exc)
        source = str(failure[ATTR_FAILURE_SOURCE])
        event = {**failure, **event}
    if error_code:
        event[c.ERROR_CODE] = error_code
    queue_wait = seconds_between(created_at, processing_started)
    processing = seconds_between(processing_started, processing_ended or now)
    if processing_started is not None and queue_wait is not None and processing is not None:
        event[ATTR_QUEUE_WAIT_MS] = queue_wait * 1000.0
        event[ATTR_PROCESSING_MS] = processing * 1000.0
    end_to_end = seconds_between(created_at, now)
    record_event(
        EVENT_JOB_FINISHED,
        event,
        duration_ms=end_to_end * 1000.0 if end_to_end is not None else None,
    )
    if processing_started is not None:
        _record_duration(PHASE_PROCESSING, processing, outcome)
    _record_duration(PHASE_END_TO_END, end_to_end, outcome)
    try:
        _instrument("counter", METRIC_JOBS_FINISHED, unit="{job}", description="Jobs that finished").add(
            1,
            {c.OUTCOME: outcome, c.ERROR_CODE: error_code or "none", ATTR_FAILURE_SOURCE: source},
        )
    except Exception as error:
        logger.warning("Could not count finished job (%s)", type(error).__name__)


# AI usage (OBS-5) -----------------------------------------------------------------------


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def billed_audio_seconds(azure_seconds: object, estimated_seconds: object) -> tuple[float | None, str]:
    """The audio seconds to record as a job's Speech usage, and where they came from.

    Azure's own duration (from its results file, what it bills on) when it is known,
    else the duration given when the job was submitted (an estimate), else nothing.
    """
    seconds = _seconds(azure_seconds)
    if seconds is not None:
        return seconds, DURATION_SOURCE_AZURE
    seconds = _seconds(estimated_seconds)
    if seconds is not None and seconds <= MAX_ESTIMATED_AUDIO_SECONDS:
        return seconds, DURATION_SOURCE_ESTIMATE
    return None, DURATION_SOURCE_NONE


def record_speech_usage(seconds: float | None, source: str) -> dict[str, object]:
    """Record a succeeded job's Azure Speech usage, once (OBS-5). Never raises.

    hmcts.speech.audio.duration (mode batch, feature batch_transcription, outcome
    success) when the seconds are known, and the job on transcription.speech.usage.jobs
    by source either way. Returns the attributes for job.finished: the seconds and
    their source, or nothing when no usage was recorded.
    """
    if seconds is not None:
        _record_speech_usage(seconds=seconds, mode=SpeechMode.BATCH, feature=AI_FEATURE_BATCH_TRANSCRIPTION)
    try:
        _instrument(
            "counter",
            METRIC_SPEECH_USAGE_JOBS,
            unit="{job}",
            description="Jobs whose Azure Speech usage was recorded, by duration source",
        ).add(1, {ATTR_AUDIO_DURATION_SOURCE: source})
    except Exception as exc:  # telemetry must never break the job
        logger.warning("Could not count speech usage (%s)", type(exc).__name__)
    if seconds is None:
        return {}
    return {ATTR_AUDIO_DURATION_S: seconds, ATTR_AUDIO_DURATION_SOURCE: source}


# Retries --------------------------------------------------------------------------------


HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_MIN = 500


def unwrap_retry(exc: BaseException) -> BaseException:
    """The last attempt's exception for a tenacity.RetryError; the exception otherwise."""
    last = getattr(exc, "last_attempt", None)
    if last is not None and getattr(last, "failed", False):
        cause = last.exception()
        if cause is not None:
            return cause
    return exc


def failure_code(exc: BaseException, component: str) -> str:
    """A controlled code for a failed call: never the exception's message.

    A 429 is ``<component>.throttled`` (DIAAT-282), other HTTP errors
    ``<component>.http_<status>``. Any other transport failure (read, write, protocol,
    proxy) is ``<component>.transport_error``. Azure results we could not parse
    (``BatchResultsInvalidError``) are ``speech.results_invalid``. DIAAT-283: an
    exception tagged by a post-Speech step (tag_failure) is that step's code.
    """
    tagged = _tag(exc)  # the original exception first, then a RetryError's cause
    if tagged is not None:
        return tagged[0]
    exc = unwrap_retry(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == HTTP_TOO_MANY_REQUESTS:
            return f"{component}.throttled"
        return f"{component}.http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return f"{component}.timeout"
    if isinstance(exc, httpx.ConnectError):
        return f"{component}.connect_error"
    if isinstance(exc, httpx.TransportError):
        return f"{component}.transport_error"
    own_code = getattr(exc, "speech_failure_code", None)
    if component == "speech" and isinstance(own_code, str) and _DEPENDENCY_CODE.fullmatch(own_code):
        return own_code
    return f"unhandled.{type(exc).__name__}"[:64]


# Failure attribution (DIAAT-282) --------------------------------------------------------

SOURCE_APPLICATION = "application"
SOURCE_QUEUE = "queue"
SOURCE_DEPENDENCY = "dependency"
_SOURCES = (SOURCE_APPLICATION, SOURCE_QUEUE, SOURCE_DEPENDENCY)

# Codes that always mean Azure Speech failed us: it reported the job as Failed,
# returned no transcript or one we could not parse, throttled us, or a call to it
# timed out or failed in transport (connection, read, write, protocol, proxy). Some
# Failed jobs (e.g. InvalidData) may be input problems; they stay Azure's here.
# Stuck jobs: one never started is a queue failure; one Azure started but has not
# finished is Azure's.
_DEPENDENCY_CODE = re.compile(
    r"speech\.(job_failed|results_unavailable|results_invalid"
    r"|throttled|timeout|connect_error|transport_error)"
)
# An HTTP error from Azure Speech: attributed by its status (_dependency_status).
_SPEECH_HTTP_CODE = re.compile(r"speech\.http_([0-9]{3})")
_STUCK_SOURCES = {"job.stuck.waiting": SOURCE_QUEUE, "job.stuck.running": SOURCE_DEPENDENCY}
# DIAAT-283: failures after Azure Speech returned the transcript. Our code, our
# database and our blob storage: the application's.
_OWN_CODE = re.compile(r"transcript\.postprocess_failed|storage\.(db_unavailable|db_rejected|db_failed|blob_failed)")
HTTP_REQUEST_TIMEOUT = 408


def _dependency_status(status: int) -> bool:
    """5xx, 408 and 429 are Azure's; any other 4xx is our request or configuration."""
    return status >= _HTTP_SERVER_ERROR_MIN or status in (
        HTTP_REQUEST_TIMEOUT,
        HTTP_TOO_MANY_REQUESTS,
    )


def _response_status(exc: BaseException | None) -> int | None:
    response = getattr(unwrap_retry(exc), "response", None) if exc is not None else None
    return int(response.status_code) if isinstance(response, httpx.Response) else None


def _code_source(error_code: str) -> str | None:
    """The source a code names by itself; None for a code attributed by its cause."""
    if error_code in _STUCK_SOURCES:
        return _STUCK_SOURCES[error_code]
    if _OWN_CODE.fullmatch(error_code):
        return SOURCE_APPLICATION
    if _DEPENDENCY_CODE.fullmatch(error_code):
        return SOURCE_DEPENDENCY
    http = _SPEECH_HTTP_CODE.fullmatch(error_code)
    if http is not None:
        return SOURCE_DEPENDENCY if _dependency_status(int(http.group(1))) else SOURCE_APPLICATION
    return None


def failure_source(error_code: str, exc: BaseException | None = None) -> str:
    """Where a failure came from: ``application``, ``queue`` or ``dependency``.

    Dependency (Azure Speech): a 5xx, 408 or 429 (``speech.throttled``) from it, a
    timeout, connection or other transport failure, a job it reports as Failed
    (``speech.job_failed``), and results it did not return or returned malformed.
    Application: any other 4xx from Azure Speech (``speech.http_400``/``401``/``403``/
    ``404``/``413``/``415``...): our payload, key, URL or configuration.

    ``speech.submission_rejected`` is attributed by the HTTP status of the response
    ``exc`` carries, by the same rule. When there is no status (e.g. Azure accepted
    the job but sent no Location header) it stays the dependency's.

    Generic codes (``job.submission_error``, ``speech.results_error``, a failed poll's
    ``unhandled.*``) are attributed by their cause, by the same rule; anything that is
    not an Azure Speech failure (our code, our database) is the application's.

    DIAAT-283: failures after Azure Speech returned the transcript
    (``transcript.postprocess_failed``, ``storage.db_*``, ``storage.blob_failed``) are
    the application's: our code, our database and our blob storage.
    """
    tagged = _tag(exc)
    if tagged is not None and tagged[0] == error_code:
        return tagged[1]  # the step that failed said where (DIAAT-283)
    if error_code == "speech.submission_rejected":
        status = _response_status(exc)
        if status is None or _dependency_status(status):
            return SOURCE_DEPENDENCY
        return SOURCE_APPLICATION
    source = _code_source(error_code)
    if source is not None:
        return source
    if exc is not None:
        return _code_source(failure_code(exc, "speech")) or SOURCE_APPLICATION
    return SOURCE_APPLICATION


# After Azure Speech (DIAAT-283) ----------------------------------------------------------

CODE_POSTPROCESS_FAILED = "transcript.postprocess_failed"
CODE_DB_UNAVAILABLE = "storage.db_unavailable"
CODE_DB_REJECTED = "storage.db_rejected"
CODE_DB_FAILED = "storage.db_failed"
CODE_BLOB_FAILED = "storage.blob_failed"
# Set on an exception by the step that failed, so job.poll_failed carries that step's
# code and source (failure_code / failure_source read them) instead of
# unhandled.<Type>. The exception is otherwise unchanged and re-raised.
_FAILURE_CODE_TAG = "hmcts_failure_code"
_FAILURE_SOURCE_TAG = "hmcts_failure_source"
_TAG_CODE = re.compile(r"[a-z][a-z0-9_.]{0,63}")


def tag_failure(exc: BaseException, error_code: str, source: str | None = None) -> None:
    """Remember the controlled code (and source) ``exc`` stands for. Never raises."""
    try:
        setattr(exc, _FAILURE_CODE_TAG, error_code)
        setattr(exc, _FAILURE_SOURCE_TAG, source or failure_source(error_code))
    except Exception as error:
        logger.warning("Could not tag the failure (%s)", type(error).__name__)


def _tag(exc: BaseException | None) -> tuple[str, str] | None:
    """The (code, source) a step tagged on ``exc`` or, for a RetryError, its cause."""
    for candidate in (exc, unwrap_retry(exc)) if exc is not None else ():
        code = getattr(candidate, _FAILURE_CODE_TAG, None)
        source = getattr(candidate, _FAILURE_SOURCE_TAG, None)
        if isinstance(code, str) and _TAG_CODE.fullmatch(code) and source in _SOURCES:
            return code, source
    return None


def db_failure_code(exc: BaseException) -> str:
    """A controlled code for a failed database write, from the exception's type.

    ``storage.db_unavailable``: the database could not be reached or used (connection,
    pool timeout, lock or deadlock: SQLAlchemy OperationalError / InterfaceError /
    TimeoutError). ``storage.db_rejected``: the database refused the data (integrity or
    data errors); retrying will not help. ``storage.db_failed``: anything else.
    """
    if isinstance(exc, (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError)):
        return CODE_DB_UNAVAILABLE
    if isinstance(exc, (sa_exc.IntegrityError, sa_exc.DataError)):
        return CODE_DB_REJECTED
    return CODE_DB_FAILED


def record_storage_failure(operation: str, error_code: str, attributes: Mapping[str, object] | None = None) -> None:
    """job.storage_failed: a database or blob write recording a job's state failed."""
    record_event(
        EVENT_STORAGE_FAILED,
        {
            **(attributes or {}),
            ATTR_STORAGE_OPERATION: operation,
            ATTR_FAILURE_SOURCE: failure_source(error_code),
            c.ERROR_CODE: error_code,
        },
    )


@contextmanager
def failing_step(
    error_code: str | Callable[[BaseException], str],
    *,
    stages: Iterable[StageHandle | None] = (),
    operation: str | None = None,
    attributes: Mapping[str, object] | None = None,
) -> Iterator[None]:
    """Attribute an exception leaving the block to one controlled code. Telemetry only.

    The exception is tagged (tag_failure), each stage in ``stages`` is marked as an
    error with the code and, for a storage write (``operation``), job.storage_failed is
    recorded. The exception is then re-raised unchanged, so behaviour is unchanged.
    """
    try:
        yield
    except Exception as exc:
        code = error_code(exc) if callable(error_code) else error_code
        tag_failure(exc, code)
        for handle in stages:
            if handle is not None:
                handle.error(code)
        if operation is not None:
            record_storage_failure(operation, code, attributes)
        raise


def storage_write(
    operation: str,
    error_code: str | Callable[[BaseException], str] = db_failure_code,
    *,
    stages: Iterable[StageHandle | None] = (),
) -> AbstractContextManager[None]:
    """failing_step for a database (default) or blob write after Azure Speech."""
    return failing_step(error_code, stages=stages, operation=operation)


def _speaker(entry: object) -> object:
    return entry.get("speaker") if isinstance(entry, Mapping) else getattr(entry, "speaker", None)


def transcript_counts(entries: Iterable[object]) -> dict[str, object]:
    """Number of dialogue entries and of distinct speakers. Never the text or names."""
    try:
        entries = list(entries)
        speakers = {_speaker(entry) for entry in entries} - {None}
        return {ATTR_SEGMENT_COUNT: len(entries), ATTR_SPEAKER_COUNT: len(speakers)}
    except Exception as exc:
        logger.warning("Could not count the transcript (%s)", type(exc).__name__)
        return {}


def record_transcript_available(
    entries: Iterable[object],
    speech_finished: datetime | None,
    polled_at: datetime,
    attributes: Mapping[str, object] | None = None,
) -> None:
    """transcript.available, with the time from Azure finishing to the transcript saved.

    ``speech_finished`` is Azure's lastActionDateTime for the Succeeded job; without it,
    the time of the poll that saw Succeeded. Includes our poll lag, the download,
    post-processing, the save and any polls whose post-processing or save failed.
    """
    ready = seconds_between(speech_finished or polled_at, datetime.now(UTC))
    record_event(
        EVENT_TRANSCRIPT_AVAILABLE,
        {**(attributes or {}), **transcript_counts(entries)},
        duration_ms=ready * 1000.0 if ready is not None else None,
    )
    _record_duration(PHASE_TO_AVAILABLE, ready)


_AZURE_ERROR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,63}")


def azure_error_code(value: object) -> str | None:
    """Azure's error code if it is a plain identifier (e.g. InvalidData); else None."""
    return value if isinstance(value, str) and _AZURE_ERROR_CODE.fullmatch(value) else None


def failed_job_error_code(status_data: Mapping[str, Any]) -> str | None:
    """``properties.error.code`` of a job Azure reports as Failed. Never the message."""
    properties = status_data.get("properties")
    error = properties.get("error") if isinstance(properties, Mapping) else None
    return azure_error_code(error.get("code")) if isinstance(error, Mapping) else None


def response_error_code(response: httpx.Response) -> str | None:
    """The ``code`` (or ``error.code``) of an Azure Speech error body. Never the message."""
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, Mapping):
        return None
    code = body.get("code")
    if code is None and isinstance(body.get("error"), Mapping):
        code = body["error"].get("code")
    return azure_error_code(code)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Retry-After (seconds or an HTTP date), or retry-after-ms, in seconds; else None."""
    headers = response.headers
    value = headers.get("Retry-After")
    if isinstance(value, str) and value.strip():
        try:
            seconds = float(value.strip())
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError):
                return None
            seconds = max(0.0, seconds)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    value = headers.get("retry-after-ms")
    if isinstance(value, str):
        try:
            millis = float(value.strip())
        except ValueError:
            return None
        return millis / 1000.0 if math.isfinite(millis) and millis >= 0 else None
    return None


def response_failure_details(response: httpx.Response) -> dict[str, object]:
    """HTTP status, Azure's error code and (when throttled) Retry-After of a response."""
    details: dict[str, object] = {ATTR_HTTP_STATUS: int(response.status_code)}
    code = response_error_code(response)
    if code is not None:
        details[ATTR_SPEECH_ERROR_CODE] = code
    if response.status_code == HTTP_TOO_MANY_REQUESTS:
        retry_after = retry_after_seconds(response)
        if retry_after is not None:
            details[ATTR_RETRY_AFTER_S] = retry_after
    return details


def failure_attributes(error_code: str, exc: BaseException | None = None) -> dict[str, object]:
    """hmcts.failure.source, plus what Azure said when ``exc`` carries its response.

    Never raises: a detail that cannot be read is left out.
    """
    attributes: dict[str, object] = {ATTR_FAILURE_SOURCE: failure_source(error_code, exc)}
    if exc is None:
        return attributes
    try:
        response = getattr(unwrap_retry(exc), "response", None)
        if isinstance(response, httpx.Response):
            attributes.update(response_failure_details(response))
    except Exception as error:
        logger.warning("Could not read the failed response (%s)", type(error).__name__)
    return attributes


def record_retry(
    operation: str,
    attempt: int,
    error_code: str,
    attributes: Mapping[str, object] | None = None,
) -> None:
    """A call for the current job failed and is about to be retried."""
    record_event(
        EVENT_JOB_RETRIED,
        {
            **(attributes or {}),
            ATTR_RETRY_OPERATION: operation,
            ATTR_RETRY_ATTEMPT: attempt,
            c.ERROR_CODE: error_code,
        },
    )
    try:
        _instrument("counter", METRIC_JOB_RETRIES, unit="{retry}", description="Retried calls").add(
            1, {ATTR_RETRY_OPERATION: operation, c.ERROR_CODE: error_code}
        )
    except Exception as exc:
        logger.warning("Could not count retry (%s)", type(exc).__name__)


_SPEECH_OPERATIONS = {
    "submit_batch_job": "speech.submit",
    "get_batch_job_status": "speech.status",
    "get_batch_results": "speech.results",
    "delete_batch_job": "speech.delete",
}


def record_speech_retry(retry_state: Any) -> None:
    """tenacity ``before_sleep`` hook for the Azure Speech client's retry policy."""
    try:
        operation = _SPEECH_OPERATIONS.get(getattr(retry_state.fn, "__name__", ""))
        exc = retry_state.outcome.exception() if retry_state.outcome is not None else None
        if operation is None or exc is None:
            return
        code = failure_code(exc, "speech")
        attributes = failure_attributes(code, exc)
        # DIAAT-282: status, results and delete calls are for a known Azure job.
        job_url = retry_state.kwargs.get("job_url") or next(iter(retry_state.args), None)
        if operation != "speech.submit" and isinstance(job_url, str):
            attributes[c.DEPENDENCY_REF] = batch_job_id_from_url(job_url)
        record_retry(operation, retry_state.attempt_number, code, attributes)
    except Exception as exc:  # never change the retry behaviour
        logger.warning("Could not record retry (%s)", type(exc).__name__)


# Stuck jobs -----------------------------------------------------------------------------


def _cutoffs(now: datetime) -> tuple[datetime, datetime]:
    settings = get_settings()
    return (
        now - timedelta(seconds=settings.JOB_STUCK_WAITING_SECONDS),
        now - timedelta(seconds=settings.JOB_STUCK_RUNNING_SECONDS),
    )


def report_stuck_jobs(session: Session, now: datetime | None = None) -> int:
    """Emit job.stuck for each job newly past its threshold. Observation only.

    Each job is claimed (stuck_reported_at) before it is reported, so the event is
    emitted once per job however many replicas poll. Returns the number reported.
    """
    now = now or datetime.now(UTC)
    waiting_cutoff, running_cutoff = _cutoffs(now)
    stuck = claim_stuck_jobs(session, waiting_cutoff=waiting_cutoff, running_cutoff=running_cutoff)
    for job in stuck:
        running = JobStatus(job.status) == JobStatus.RUNNING
        state = "running" if running else "waiting"
        since = job.processing_started_datetime if running else None
        age = seconds_between(since or job.created_datetime, now)
        attributes: dict[str, object] = {
            ATTR_JOB_STATE: state,
            ATTR_FAILURE_SOURCE: failure_source(f"job.stuck.{state}"),
        }
        if job.batch_job_id:
            attributes[c.DEPENDENCY_REF] = job.batch_job_id
        with journey(JOURNEY_JOB, str(job.id), parent_id=upload_id_from_blob(job.audio_blob_path)):
            record_event(
                EVENT_JOB_STUCK,
                attributes,
                duration_ms=age * 1000.0 if age is not None else None,
            )
        logger.warning("Job %s is stuck: %s for %.0f s", job.id, state, age or 0.0)
    return len(stuck)


# Queue gauges ---------------------------------------------------------------------------


class JobStateGauges:
    """Observable gauges of the job "queue", computed from the transcription_job table.

    Multiple replicas: every replica runs the same query and so reports the same
    database-wide value. Dashboards and alerts must aggregate these gauges with max
    (or take any one replica's value), never sum, or the numbers multiply by the
    replica count.

    Cost: one aggregate query, at most once per ``max_age_s``, shared by all five
    gauges (they are observed together on each export, every 60 s by default). A
    database error is logged and that collection is skipped: no values are reported,
    and neither the export nor the app fails. A failure is also cached for
    ``max_age_s`` so a database outage is not queried once per gauge.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        max_age_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._max_age_s = max_age_s
        self._clock = clock
        self._lock = threading.Lock()
        self._fetched_at: float | None = None
        self._counts: JobStateCounts | None = None

    def snapshot(self) -> JobStateCounts | None:
        with self._lock:
            if self._fetched_at is not None and self._clock() - self._fetched_at < self._max_age_s:
                return self._counts
            self._fetched_at = self._clock()
            try:
                waiting_cutoff, running_cutoff = _cutoffs(datetime.now(UTC))
                with self._session_factory() as session:
                    self._counts = count_job_states(
                        session, waiting_cutoff=waiting_cutoff, running_cutoff=running_cutoff
                    )
            except Exception as exc:
                logger.warning("Job state gauges skipped: query failed (%s)", type(exc).__name__)
                self._counts = None
            return self._counts

    def _observations(self, values: Callable[[JobStateCounts], Iterable[Observation]]):
        def callback(_options: CallbackOptions) -> Iterable[Observation]:
            counts = self.snapshot()
            return list(values(counts)) if counts is not None else []

        return callback

    def register(self, meter: Meter) -> None:
        meter.create_observable_gauge(
            METRIC_JOBS_WAITING,
            callbacks=[self._observations(lambda n: [Observation(n.waiting)])],
            unit="{job}",
            description="Jobs not yet processed by Azure Speech (pending or submitted)",
        )
        meter.create_observable_gauge(
            METRIC_JOBS_RUNNING,
            callbacks=[self._observations(lambda n: [Observation(n.running)])],
            unit="{job}",
            description="Jobs being processed by Azure Speech",
        )
        meter.create_observable_gauge(
            METRIC_QUEUE_DEPTH,
            callbacks=[self._observations(lambda n: [Observation(n.waiting + n.running)])],
            unit="{job}",
            description="Unfinished jobs: waiting plus running",
        )
        meter.create_observable_gauge(
            METRIC_OLDEST_WAITING_AGE,
            callbacks=[self._observations(self._oldest_waiting_age)],
            unit="s",
            description="Age of the oldest waiting job not yet reported stuck (0 when none are waiting)",
        )
        meter.create_observable_gauge(
            METRIC_JOBS_STUCK,
            callbacks=[
                self._observations(
                    lambda n: [
                        Observation(n.stuck_waiting, {ATTR_JOB_STATE: "waiting"}),
                        Observation(n.stuck_running, {ATTR_JOB_STATE: "running"}),
                    ]
                )
            ],
            unit="{job}",
            description="Jobs waiting or running longer than their stuck threshold",
        )
        limit = get_settings().SPEECH_CONCURRENT_JOBS_LIMIT
        if limit:
            # DIAAT-282: how close Azure Speech is to its concurrent-jobs limit (1 = at it).
            meter.create_observable_gauge(
                METRIC_SPEECH_UTILISATION,
                callbacks=[self._observations(lambda n: [Observation(n.running / limit)])],
                unit="1",
                description="Jobs running in Azure Speech as a fraction of SPEECH_CONCURRENT_JOBS_LIMIT",
            )

    @staticmethod
    def _oldest_waiting_age(counts: JobStateCounts) -> list[Observation]:
        """Seconds since the oldest waiting job not yet reported stuck was created.

        Jobs already reported stuck are left out: ``transcription.jobs.stuck`` and
        ``job.stuck`` cover them, and one that never finishes would otherwise hold this
        early-warning gauge high for good.
        """
        age = seconds_between(counts.oldest_waiting_created, datetime.now(UTC))
        return [Observation(age or 0.0)]


class PollerHeartbeat:
    """DIAAT-277: is the polling loop (the job "queue"'s worker) alive?

    ``transcription.poller.last_success_age``: seconds since the loop last completed a
    poll without an unexpected error, or since it started if it has not yet. Growing
    beyond a few poll intervals means the loop is stuck or failing on every poll.
    Nothing is observed until the loop has started (e.g. in tests). Per replica: each
    runs its own loop, so aggregate with max (any replica stalled) or min (all).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._since: float | None = None

    def started(self) -> None:
        with self._lock:
            if self._since is None:
                self._since = self._clock()

    def succeeded(self) -> None:
        with self._lock:
            self._since = self._clock()

    def age(self) -> float | None:
        with self._lock:
            return None if self._since is None else max(0.0, self._clock() - self._since)

    def _observe(self, _options: CallbackOptions) -> list[Observation]:
        age = self.age()
        return [] if age is None else [Observation(age)]

    def register(self, meter: Meter) -> None:
        meter.create_observable_gauge(
            METRIC_POLLER_LAST_SUCCESS_AGE,
            callbacks=[self._observe],
            unit="s",
            description="Seconds since the job polling loop last completed a poll",
        )


poller_heartbeat = PollerHeartbeat()

_registered: list[Meter] = []


def register_job_gauges(session_factory: Callable[[], Session] | None = None) -> bool:
    """Register the job-state gauges on the current meter provider, once. True if new."""
    meter = get_meter()
    if any(existing is meter for existing in _registered):
        return False
    if session_factory is None:
        from transcribe_api.runtime.db_recording import get_engine

        def session_factory() -> Session:
            return Session(get_engine())

    JobStateGauges(session_factory).register(meter)
    poller_heartbeat.register(meter)
    _registered.append(meter)
    return True
