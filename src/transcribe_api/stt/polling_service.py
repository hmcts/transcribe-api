"""BatchPollingService: polls Azure Batch Transcription and dispatches webhooks.

On each interval the service:
1. Queries for TranscriptionJobs in NOT_STARTED or RUNNING batch state using
   SELECT FOR UPDATE SKIP LOCKED (safe for multi-replica deployments).
2. Polls Azure for the current job status.
3. On Succeeded: downloads results, runs speaker processing, saves results,
   dispatches webhook to callback_url (if registered).
4. On Failed: records the error, dispatches failure webhook.
5. Deletes the Azure batch job after completion.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID

from hmcts_otel import StageHandle, journey, record_event, stage
from hmcts_otel import conventions as c
from sqlmodel import Session

from transcribe_api.domain import interface_recording as jobs_db
from transcribe_api.domain.interface_recording import (
    claim_webhook_dispatch,
    fetch_pending_batch_jobs,
    mark_job_error,
    mark_needs_cleanup,
    save_job_results,
    update_job_batch_status,
)
from transcribe_api.domain.models_recording import BatchJobStatus, JobStatus
from transcribe_api.domain.provenance_recording import TranscriptProvenance, build_provenance
from transcribe_api.domain.webhook import dispatch
from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager
from transcribe_api.runtime.db_recording import get_engine
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import (
    ATTR_FAILURE_SOURCE,
    ATTR_SPEECH_ERROR_CODE,
    ATTR_WEBHOOK_NOTIFICATION,
    EVENT_JOB_FAILED_IN_SPEECH,
    EVENT_JOB_PART_FINISHED,
    EVENT_JOB_POLL_FAILED,
    EVENT_WEBHOOK_DELIVERED,
    EVENT_WEBHOOK_DELIVERY_FAILED,
    JOURNEY_JOB,
    STAGE_PERSISTENCE,
    STAGE_POSTPROCESSING,
    STAGE_RESULTS_RETRIEVAL,
    batch_job_id_from_url,
    upload_id_from_blob,
)
from transcribe_api.stt import job_telemetry
from transcribe_api.stt.batch_client import (
    BatchResultError,
    BatchResultsInvalidError,
    delete_batch_job,
    get_batch_job_status,
    get_batch_results,
    get_model_details,
)
from transcribe_api.stt.part_merge import PartResult, billed_seconds, merge_parts
from transcribe_api.stt.part_submission import cleanup_job_parts
from transcribe_api.stt.speakers_recording import process_speakers, speaker_step_report
from transcribe_api.stt.speech_telemetry import speech_config_attributes

logger = logging.getLogger(__name__)

_POLL_BATCH_SIZE = 10


@dataclass(frozen=True)
class _PendingJob:
    id: UUID
    batch_job_url: str
    batch_job_status: BatchJobStatus
    callback_url: str | None
    caller_id: UUID | None
    metadata_: dict
    webhook_secret: str | None = None
    trace_parent: str | None = None
    audio_blob_path: str | None = None
    locale: str = "en-GB"
    created_datetime: datetime | None = None
    processing_started_datetime: datetime | None = None
    enable_diarization: bool | None = None
    # OBS-5: the duration given when the job was submitted (an estimate), the fallback
    # for the Speech usage recorded when Azure's results do not carry one.
    audio_duration_seconds: float | None = None


@dataclass(frozen=True)
class _PendingPart:
    """A part of a recording in parts, to be polled (DIAAT-408), with what its job's events need."""

    id: UUID
    job_id: UUID
    part_index: int
    part_count: int
    batch_job_url: str
    batch_job_status: str | None
    job_created_datetime: datetime | None
    audio_blob_path: str | None


def _part_failed_message(index: int, count: int) -> str:
    return f"Part {index} of {count} of the recording could not be transcribed. Please upload the recording again."


def _part_results_message(index: int, count: int) -> str:
    return (
        f"The transcript of part {index} of {count} of the recording could not be retrieved. "
        "Please upload the recording again."
    )


def _extract_model_identifier(status_data: dict, locale: str) -> str:
    """Best-effort model identifier from Azure's batch job status response.

    Azure returns a `model.self` URL identifying the exact model resource
    used (e.g. ".../speechtotext/v3.2/models/base/<guid>") once a job has
    completed. Nothing in this service pins a specific custom model at
    submission time, so when Azure omits the field (older API versions,
    mocked responses) fall back to a locale-qualified label identifying the
    engine in use.
    """
    model = status_data.get("model")
    if isinstance(model, dict):
        model_self = model.get("self")
        if isinstance(model_self, str) and model_self:
            return model_self
    return f"azure-speech-batch-transcription ({locale})"


def _compose_model_display_name(model_details: dict) -> str | None:
    """Build a human-readable model label from a Speech model resource.

    Prefers Azure's `displayName`, qualifying it with `locale` when present
    (e.g. "20240614 Base — en-GB"). Returns None when there's no usable
    `displayName` so callers fall back to the raw model_identifier. Parses
    defensively: every field may be absent on older API versions or partial
    responses.
    """
    display_name = model_details.get("displayName")
    if not isinstance(display_name, str) or not display_name.strip():
        return None
    display_name = display_name.strip()

    locale = model_details.get("locale")
    if isinstance(locale, str) and locale.strip():
        return f"{display_name} — {locale.strip()}"
    return display_name


def _is_trusted_speech_url(model_identifier: str) -> bool:
    """True only when model_identifier is an HTTPS URL on our Speech host.

    Our Speech credentials (an Entra bearer token, or the subscription key in
    local dev) travel in the request header, so we must never dereference an
    arbitrary URL: a malformed or off-Azure `model.self` would otherwise
    exfiltrate them (SSRF). We require HTTPS and an exact host
    match against the configured AZURE_SPEECH_ENDPOINT — the only host our
    Speech credentials are valid for. Anything else (non-URL fallback labels,
    http, a different host) is rejected and the caller falls back to the raw
    model_identifier.
    """
    endpoint = get_settings().AZURE_SPEECH_ENDPOINT
    if not endpoint:
        return False
    try:
        parsed = urlparse(model_identifier)
        expected = urlparse(endpoint)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() == (expected.hostname or "").lower()
    )


async def _resolve_model_display_name(model_identifier: str) -> str | None:
    """Best-effort resolution of a model's friendly name from its self URL.

    model_identifier is Azure's `model.self` URL when the batch response
    carried one, otherwise a non-URL fallback label (which can't be
    dereferenced). Resolution is skipped unless the identifier is an HTTPS URL
    on our configured Speech host (see `_is_trusted_speech_url`) so our
    Speech credentials are never sent anywhere else. Any failure — untrusted or
    non-URL identifier, network error, 401, deleted model — is caught and
    logged and yields None so job completion and the metadata display are
    never broken (DIAAT-243 AC5).
    """
    if not _is_trusted_speech_url(model_identifier):
        return None
    try:
        model_details = await get_model_details(model_identifier)
    except Exception as exc:
        logger.warning("Could not resolve model display name (%s)", type(exc).__name__)
        return None
    return _compose_model_display_name(model_details)


def _extract_transcription_duration_seconds(
    status_data: dict, fallback_start: datetime | None
) -> float | None:
    """How long the transcription itself took to produce.

    Prefers Azure's own `createdDateTime`/`lastActionDateTime` timestamps
    (present on real batch job status responses) since they reflect Azure's
    actual processing window. Falls back to wall-clock time since this
    service first created the job record when Azure's timestamps are
    missing or unparseable.

    Both paths clamp to 0: a negative value is never a meaningful duration
    and can arise from clock skew (e.g. the worker's clock running behind
    the job-creation timestamp on the wall-clock path).
    """
    created = status_data.get("createdDateTime")
    last_action = status_data.get("lastActionDateTime")
    if isinstance(created, str) and isinstance(last_action, str):
        try:
            start = datetime.fromisoformat(created)
            end = datetime.fromisoformat(last_action)
            duration = (end - start).total_seconds()
            if duration >= 0:
                return duration
        except ValueError:
            pass

    if fallback_start is not None:
        return max(0.0, (datetime.now(UTC) - fallback_start).total_seconds())

    return None


def _job_finished(
    job: _PendingJob,
    status_data: dict,
    outcome: str,
    error_code: str | None = None,
    *,
    exc: BaseException | None = None,
    attributes: dict[str, object] | None = None,
) -> None:
    """job.finished with the job's queue wait, processing and end-to-end time (DIAAT-281).

    DIAAT-282: also the Azure Speech job ID, the configuration reference and, for a
    failure, where it came from (see job_telemetry.failure_source).
    """
    job_telemetry.record_job_finished(
        outcome,
        error_code,
        created_at=job.created_datetime,
        processing_started=job.processing_started_datetime,
        processing_ended=job_telemetry.azure_last_action(status_data),
        exc=exc,
        attributes={
            **speech_config_attributes(job.locale, job.enable_diarization, status_data),
            c.DEPENDENCY_REF: batch_job_id_from_url(job.batch_job_url),
            **(attributes or {}),
        },
    )


def _speech_job_failed(job: _PendingJob, status_data: dict) -> None:
    """speech.job_failed and job.finished for a job Azure reports as Failed.

    Azure's error text is stored on the job, never exported in telemetry.
    DIAAT-282: its error code (e.g. InvalidData) is, when it is one.
    """
    azure_error = {ATTR_FAILURE_SOURCE: job_telemetry.failure_source("speech.job_failed")}
    code = job_telemetry.failed_job_error_code(status_data)
    if code is not None:
        azure_error[ATTR_SPEECH_ERROR_CODE] = code
    record_event(
        EVENT_JOB_FAILED_IN_SPEECH,
        {
            c.DEPENDENCY_REF: batch_job_id_from_url(job.batch_job_url),
            **azure_error,
            c.ERROR_CODE: "speech.job_failed",
        },
    )
    _job_finished(job, status_data, "error", "speech.job_failed", attributes=azure_error)


def _record_delivery(delivered: bool, notification: str) -> None:
    """webhook.delivered or webhook.delivery_failed (DIAAT-281), with what it carried."""
    if delivered:
        record_event(EVENT_WEBHOOK_DELIVERED, {ATTR_WEBHOOK_NOTIFICATION: notification})
    else:
        record_event(
            EVENT_WEBHOOK_DELIVERY_FAILED,
            {ATTR_WEBHOOK_NOTIFICATION: notification, c.ERROR_CODE: "webhook.not_delivered"},
        )


# DIAAT-408: recordings in parts ---------------------------------------------------------------------
#
# A recording over 240 minutes is several Azure jobs (transcription_job_part); the job has none of its
# own, so it is polled through its part rows. Each poll takes the parts polled longest ago (SKIP
# LOCKED, so two pollers take different parts; a poll stamps the part), records Running/Succeeded/
# Failed, and then looks for jobs whose parts have all succeeded and assembles them. See
# domain/interface_recording.py for the conditional writes that make a late answer harmless.


class _PartPolling:
    """The multipart half of BatchPollingService (a mixin: it uses its settings and webhook helpers)."""

    settings: object
    _resolve_webhook_secret: object

    async def _poll_parts_once(self) -> None:
        parts = await asyncio.to_thread(self._fetch_pending_parts)
        if parts:
            logger.info("Checking %d part(s) of recordings in parts", len(parts))
            await asyncio.gather(*[self._process_part(part) for part in parts], return_exceptions=True)
        for job_id in await asyncio.to_thread(self._fetch_ready_jobs):
            try:
                await self._assemble(job_id)
            except Exception as exc:
                self._poll_failed(job_id, None, exc)
        for job_id in await asyncio.to_thread(self._fetch_leftovers):
            await self._cleanup_parts(job_id)

    # Reading ---------------------------------------------------------------------------------

    def _fetch_pending_parts(self) -> list[_PendingPart]:
        with Session(get_engine()) as session:
            rows = jobs_db.fetch_pending_parts(session)
            by_job: dict[UUID, object] = {}
            result = []
            for row in rows:
                if row.job_id not in by_job:
                    by_job[row.job_id] = jobs_db.get_job_by_id(session, row.job_id)
                job = by_job[row.job_id]
                result.append(
                    _PendingPart(
                        id=row.id,
                        job_id=row.job_id,
                        part_index=row.part_index,
                        part_count=row.part_count,
                        batch_job_url=row.batch_job_url,
                        batch_job_status=row.batch_job_status,
                        job_created_datetime=job.created_datetime if job else None,
                        audio_blob_path=job.audio_blob_path if job else None,
                    )
                )
            return result

    def _fetch_ready_jobs(self) -> list[UUID]:
        with Session(get_engine()) as session:
            return jobs_db.fetch_jobs_ready_to_assemble(session)

    def _fetch_leftovers(self) -> list[UUID]:
        with Session(get_engine()) as session:
            return jobs_db.fetch_jobs_with_part_leftovers(session)

    # One part --------------------------------------------------------------------------------

    def _poll_failed(self, job_id: UUID, speech_ref: dict | None, exc: BaseException) -> None:
        logger.error("Error polling parts of job %s (%s)", job_id, type(exc).__name__, exc_info=exc)
        code = job_telemetry.failure_code(exc, "speech")
        failure = job_telemetry.failure_attributes(code, exc)
        record_event(EVENT_JOB_POLL_FAILED, {**failure, **(speech_ref or {}), c.ERROR_CODE: code})

    async def _process_part(self, part: _PendingPart) -> None:
        # DIAAT-276: everything emitted while handling this part carries its job's journey.
        with journey(JOURNEY_JOB, str(part.job_id), parent_id=upload_id_from_blob(part.audio_blob_path)):
            speech_ref = {
                c.DEPENDENCY_REF: batch_job_id_from_url(part.batch_job_url),
                "job.part_index": part.part_index,
                "job.part_count": part.part_count,
            }
            try:
                status_data = await get_batch_job_status(part.batch_job_url)
                azure_status = status_data.get("status", "")
                if azure_status == BatchJobStatus.RUNNING:
                    await self._part_running(part, status_data, speech_ref)
                elif azure_status == BatchJobStatus.SUCCEEDED:
                    await self._part_succeeded(part, status_data, speech_ref)
                elif azure_status == BatchJobStatus.FAILED:
                    await self._part_failed(part, status_data, speech_ref)
                else:
                    await asyncio.to_thread(self._stamp_part, part.id)  # still waiting: polled
            except Exception as exc:
                # The part stays as it was and is polled again (its stamp moves it behind the others).
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._stamp_part, part.id)
                self._poll_failed(part.job_id, speech_ref, exc)

    def _stamp_part(self, part_id: UUID) -> None:
        with Session(get_engine()) as session:
            jobs_db.record_part_polled(session, part_id)

    def _record_part(self, part_id: UUID, status: str, batch_status: BatchJobStatus) -> bool:
        with Session(get_engine()) as session:
            return jobs_db.record_part_polled(session, part_id, status=status, batch_status=batch_status)

    def _job_running(self, job_id: UUID, started: datetime) -> bool:
        with Session(get_engine()) as session:
            return jobs_db.mark_job_running(session, job_id, started)

    async def _part_running(self, part: _PendingPart, status_data: dict, speech_ref: dict) -> None:
        if part.batch_job_status != BatchJobStatus.NOT_STARTED.value:
            await asyncio.to_thread(self._stamp_part, part.id)
            return
        started = job_telemetry.processing_started_at(status_data, part.job_created_datetime)
        if not await asyncio.to_thread(self._record_part, part.id, JobStatus.RUNNING.value, BatchJobStatus.RUNNING):
            return  # the job has finished or failed meanwhile
        # The job runs from the first part that starts: its queue wait ends there, once.
        if await asyncio.to_thread(self._job_running, part.job_id, started):
            job_telemetry.record_processing_started(
                {c.DEPENDENCY_REF: speech_ref[c.DEPENDENCY_REF]}, part.job_created_datetime, started
            )

    async def _part_succeeded(self, part: _PendingPart, status_data: dict, speech_ref: dict) -> None:
        if not await asyncio.to_thread(self._record_part, part.id, JobStatus.SUCCEEDED.value, BatchJobStatus.SUCCEEDED):
            return
        processing = job_telemetry.seconds_between(
            job_telemetry.processing_started_at(status_data, part.job_created_datetime),
            job_telemetry.azure_last_action(status_data),
        )
        record_event(
            EVENT_JOB_PART_FINISHED,
            {**speech_ref, c.OUTCOME: "success"},
            duration_ms=processing * 1000.0 if processing is not None else None,
        )

    async def _part_failed(self, part: _PendingPart, status_data: dict, speech_ref: dict) -> None:
        """A part Azure reports as Failed fails the job at once (a late result of another part cannot
        bring it back: every write of the job is conditional on it still being in flight)."""
        failed = await asyncio.to_thread(
            self._fail_job_for_part, part, _part_failed_message(part.part_index, part.part_count)
        )
        if not failed:
            return
        code = job_telemetry.failed_job_error_code(status_data)
        azure_error = {ATTR_FAILURE_SOURCE: job_telemetry.failure_source("speech.job_failed")}
        if code is not None:
            azure_error[ATTR_SPEECH_ERROR_CODE] = code
        record_event(
            EVENT_JOB_FAILED_IN_SPEECH,
            {**speech_ref, **azure_error, c.ERROR_CODE: "speech.job_failed"},
        )
        record_event(
            EVENT_JOB_PART_FINISHED,
            {**speech_ref, **azure_error, c.OUTCOME: "error", c.ERROR_CODE: "speech.job_failed"},
        )
        context = await asyncio.to_thread(self._job_context, part.job_id)
        await self._finish_failed_job(
            context, _part_failed_message(part.part_index, part.part_count), "speech.job_failed", part.part_count
        )

    def _fail_job_for_part(self, part: _PendingPart, message: str) -> bool:
        with Session(get_engine()) as session:
            if not jobs_db.fail_multipart_job(session, part.job_id, message):
                return False
            jobs_db.mark_part_failed(session, part.id, "speech.job_failed")
            return True

    # The job ---------------------------------------------------------------------------------

    def _job_context(self, job_id: UUID) -> _PendingJob | None:
        """The job's fields the telemetry and the webhook need, as the single-file poller has them."""
        with Session(get_engine()) as session:
            row = jobs_db.get_job_by_id(session, job_id)
            if row is None:
                return None
            webhook_secret = None
            if row.callback_url and row.caller_id:
                try:
                    webhook_secret = self._resolve_webhook_secret(row.caller_id)
                except Exception as exc:
                    logger.error("Cannot resolve webhook secret for job %s (%s)", row.id, type(exc).__name__)
            return _PendingJob(
                id=row.id,
                batch_job_url="",
                batch_job_status=row.batch_job_status or BatchJobStatus.NOT_STARTED,
                callback_url=row.callback_url,
                caller_id=row.caller_id,
                metadata_=row.metadata_,
                webhook_secret=webhook_secret,
                trace_parent=row.trace_parent,
                audio_blob_path=row.audio_blob_path,
                locale=row.locale,
                created_datetime=row.created_datetime,
                processing_started_datetime=row.processing_started_datetime,
                enable_diarization=row.enable_diarization,
                audio_duration_seconds=row.audio_duration_seconds,
            )

    async def _finish_failed_job(
        self, job: _PendingJob | None, message: str, code: str, part_count: int, *, exc: BaseException | None = None
    ) -> None:
        """What follows the job having been marked FAILED (only the call that marked it gets here):
        job.finished, the WAV's blob metadata, the failure webhook, and the cleanup of every part."""
        if job is None:
            return
        job_telemetry.record_job_finished(
            "error",
            code,
            created_at=job.created_datetime,
            processing_started=job.processing_started_datetime,
            exc=exc,
            attributes={
                **speech_config_attributes(job.locale, job.enable_diarization),
                "job.part_count": part_count,
            },
        )
        if job.audio_blob_path:
            try:
                async with AsyncAzureBlobManager() as blob_manager:
                    await blob_manager.set_blob_metadata(
                        blob_name=job.audio_blob_path,
                        metadata={
                            "processed": "false",
                            "status": "permanently_failed",
                            "last_error": message[:1000],
                            "failed_at": datetime.now(UTC).isoformat(),
                        },
                    )
            except Exception as error:
                logger.warning("Could not mark blob as failed for job %s (%s)", job.id, type(error).__name__)
        await self._dispatch_failure(job, message)
        await self._cleanup_parts(job.id)

    async def _cleanup_parts(self, job_id: UUID) -> None:
        """Delete every part blob and Azure job of a finished or failed job. Never fails the job: what
        cannot be deleted is recorded on the job (needs_cleanup) and tried again by a later poll."""
        try:
            await cleanup_job_parts(job_id, engine=get_engine())
        except Exception as exc:
            logger.warning("Could not clean up the parts of job %s (%s)", job_id, type(exc).__name__)

    async def _assemble(self, job_id: UUID) -> None:
        """Every part has succeeded: fetch their results, merge them, save the transcript.

        Exactly once: the transcript and the finished state are written in one conditional UPDATE
        (complete_multipart_job); two pollers that assemble the same job at the same moment do the
        same work, one UPDATE changes the row, and only that call records usage, finishes the job in
        telemetry, dispatches the webhook and cleans up. A worker that dies before the UPDATE leaves
        nothing behind (the transcript appears all at once or not at all) and the next poll assembles
        the job again; one that dies after it leaves the part resources, which the leftover sweep
        deletes."""
        context = await asyncio.to_thread(self._job_context, job_id)
        rows = await asyncio.to_thread(self._part_rows, job_id)
        if context is None or not rows:
            return
        count = len(rows)
        with journey(JOURNEY_JOB, str(job_id), parent_id=upload_id_from_blob(context.audio_blob_path)):
            polled_at = datetime.now(UTC)
            with stage(
                "result_processing", attributes={"job.part_count": count}, link_from=context.trace_parent
            ) as processing:
                results: list[PartResult] = []
                statuses: list[dict] = []
                for row in rows:
                    part_ref = {
                        c.DEPENDENCY_REF: batch_job_id_from_url(row.batch_job_url),
                        "job.part_index": row.part_index,
                    }
                    failure: tuple[str, BaseException] | None = None
                    with stage(STAGE_RESULTS_RETRIEVAL, attributes=part_ref) as retrieval:
                        try:
                            status_data = await get_batch_job_status(row.batch_job_url)
                            entries = await get_batch_results(row.batch_job_url, transcription_job_id=job_id)
                        except BatchResultsInvalidError as exc:
                            failure = ("speech.results_invalid", exc)
                        except BatchResultError as exc:
                            failure = ("speech.results_unavailable", exc)
                        except Exception as exc:
                            failure = ("speech.results_error", exc)
                        if failure is not None:
                            retrieval.error(failure[0])
                    if failure is not None:
                        await self._fail_assembly(context, row.part_index, count, processing, *failure)
                        return
                    statuses.append(status_data)
                    results.append(
                        PartResult(
                            index=row.part_index,
                            count=count,
                            offset_seconds=row.offset_seconds,
                            duration_seconds=row.duration_seconds,
                            entries=list(entries),
                            azure_duration_seconds=getattr(entries, "audio_duration_seconds", None),
                        )
                    )

                with (
                    stage(STAGE_POSTPROCESSING) as postprocessing,
                    job_telemetry.failing_step(
                        job_telemetry.CODE_POSTPROCESS_FAILED, stages=(postprocessing, processing)
                    ),
                ):
                    with speaker_step_report() as speaker_steps:
                        merged = merge_parts(results, lambda entries, _part: process_speakers(entries))
                    first = statuses[0]
                    model_identifier = _extract_model_identifier(first, context.locale)
                    # The parts run at the same time: the job took as long as its slowest part.
                    durations = [
                        d
                        for d in (
                            _extract_transcription_duration_seconds(s, context.created_datetime) for s in statuses
                        )
                        if d is not None
                    ]
                    transcription_duration_seconds = max(durations) if durations else None

                model_display_name = await _resolve_model_display_name(model_identifier)
                provenance = build_provenance(
                    locale=context.locale,
                    diarization=context.enable_diarization,
                    status_data=first,
                    batch_job_url=rows[0].batch_job_url,  # the first part's Azure job stands for the model used
                    model_display_name=model_display_name,
                    speaker_steps=speaker_steps,
                )
                self._check_part_durations(job_id, context, rows)

                with (
                    stage(STAGE_PERSISTENCE) as persistence,
                    job_telemetry.storage_write("transcript.save", stages=(persistence, processing)),
                ):
                    won = await asyncio.to_thread(
                        self._complete,
                        job_id,
                        merged,
                        transcription_duration_seconds,
                        model_identifier,
                        model_display_name,
                        provenance,
                    )
            if not won:
                logger.info("Job %s was already assembled or has failed: nothing more to do", job_id)
                return
            usage = (
                {}
                if self.settings.SPEECH_BACKEND == "mock"
                else job_telemetry.record_speech_usage(*billed_seconds(results))
            )
            job_telemetry.record_transcript_available(
                merged,
                max((a for a in (job_telemetry.azure_last_action(s) for s in statuses) if a is not None), default=None),
                polled_at,
                {"job.part_count": count},
            )
            job_telemetry.record_job_finished(
                "success",
                None,
                created_at=context.created_datetime,
                processing_started=context.processing_started_datetime,
                processing_ended=max(
                    (a for a in (job_telemetry.azure_last_action(s) for s in statuses) if a is not None), default=None
                ),
                attributes={
                    **speech_config_attributes(context.locale, context.enable_diarization, first),
                    "job.part_count": count,
                    **usage,
                },
            )
            await self._dispatch_success(context, merged)
            await self._cleanup_parts(job_id)

    def _part_rows(self, job_id: UUID) -> list:
        with Session(get_engine()) as session:
            rows = jobs_db.get_job_parts(session, job_id)
            for row in rows:
                session.expunge(row)
            return rows

    def _check_part_durations(self, job_id: UUID, context: _PendingJob, rows: list) -> None:
        """The recording's length (``audio_duration_seconds``, kept as it is) and the sum of its parts
        agree by construction (the parts were cut from it and measured); if they ever do not, say so."""
        total = sum(row.duration_seconds for row in rows)
        recorded = context.audio_duration_seconds
        if recorded is not None and abs(total - recorded) > 1.0:
            logger.warning("Job %s: its parts add up to %.1f s, the recording is %.1f s", job_id, total, recorded)

    def _complete(
        self, job_id, merged, transcription_duration_seconds, model_identifier, model_display_name, provenance
    ) -> bool:
        with Session(get_engine()) as session:
            return jobs_db.complete_multipart_job(
                session,
                job_id,
                merged,
                transcription_duration_seconds=transcription_duration_seconds,
                model_identifier=model_identifier,
                model_display_name=model_display_name,
                provenance=provenance,
            )

    async def _fail_assembly(
        self,
        context: _PendingJob,
        part_index: int,
        count: int,
        processing: StageHandle | None,
        error_code: str,
        exc: BaseException,
    ) -> None:
        """A part's results could not be retrieved: the job FAILED, like a part Azure failed."""
        logger.error(
            "Failed to retrieve results of part %d for job %s (%s)", part_index, context.id, type(exc).__name__
        )
        if processing is not None:
            processing.error(error_code)
        message = _part_results_message(part_index, count)
        failed = await asyncio.to_thread(self._fail_job_for_job, context.id, message)
        if failed:
            await self._finish_failed_job(context, message, error_code, count, exc=exc)

    def _fail_job_for_job(self, job_id: UUID, message: str) -> bool:
        with Session(get_engine()) as session:
            return jobs_db.fail_multipart_job(session, job_id, message)


class BatchPollingService(_PartPolling):
    """Background service that polls Azure Batch Transcription job status."""

    def __init__(self, webhook_secret_resolver=None) -> None:
        self.settings = get_settings()
        self._shutdown = False
        self._resolve_webhook_secret = webhook_secret_resolver or self._default_secret_resolver

    def _default_secret_resolver(self, caller_id: UUID) -> str:
        from transcribe_api.domain.interface_recording import get_caller_by_id
        from transcribe_api.runtime.security import decrypt_webhook_secret

        with Session(get_engine()) as session:
            caller = get_caller_by_id(session, caller_id)
            if not caller:
                raise ValueError(f"Caller {caller_id} not found; cannot resolve webhook secret")
            return decrypt_webhook_secret(caller.webhook_secret)  # raises InvalidToken on bad key

    async def run_polling_loop(self) -> None:
        logger.info(
            "BatchPollingService started (interval=%ds)",
            self.settings.BATCH_POLL_INTERVAL_SECONDS,
        )
        job_telemetry.poller_heartbeat.started()
        try:
            while not self._shutdown:
                try:
                    await self._poll_once()
                    await self._poll_parts_once()  # DIAAT-408: jobs in parts, through their part rows
                    job_telemetry.poller_heartbeat.succeeded()
                except Exception as exc:
                    logger.error(
                        "Unexpected error in polling loop (%s)", type(exc).__name__, exc_info=exc
                    )
                await self._report_stuck_jobs()
                await asyncio.sleep(self.settings.BATCH_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("BatchPollingService cancelled")
            raise

    async def _poll_once(self) -> None:
        jobs = await asyncio.to_thread(self._fetch_pending_jobs)
        if not jobs:
            return
        logger.info("Checking %d pending batch job(s)", len(jobs))
        await asyncio.gather(
            *[self._process_job(job) for job in jobs],
            return_exceptions=True,
        )

    async def _report_stuck_jobs(self) -> None:
        """job.stuck for jobs past their threshold (DIAAT-281). Never fails the loop."""
        try:
            await asyncio.to_thread(self._claim_and_report_stuck_jobs)
        except Exception as exc:
            logger.warning("Stuck-job check skipped (%s)", type(exc).__name__)

    def _claim_and_report_stuck_jobs(self) -> None:
        with Session(get_engine()) as session:
            job_telemetry.report_stuck_jobs(session)

    def _fetch_pending_jobs(self) -> list[_PendingJob]:
        with Session(get_engine()) as session:
            rows = fetch_pending_batch_jobs(session)
            result = []
            for row in rows:
                if not row.batch_job_url:
                    continue
                # Webhook secrets are only needed when the job has a callback URL.
                # JWT-submitted jobs have caller_id=None and no callback_url, so
                # skip secret resolution for them — attempting it would always fail
                # and cause the job to be silently dropped from the polling queue.
                webhook_secret = None
                if row.callback_url:
                    if not row.caller_id:
                        logger.error(
                            "Job %s has callback_url but no caller_id; "
                            "skipping webhook secret resolution",
                            row.id,
                        )
                    else:
                        try:
                            webhook_secret = self._resolve_webhook_secret(row.caller_id)
                        except Exception as exc:
                            logger.error(
                                "Cannot resolve webhook secret for caller %s (job %s skipped) (%s)",
                                row.caller_id,
                                row.id,
                                type(exc).__name__,
                                exc_info=exc,
                            )
                            continue
                result.append(
                    _PendingJob(
                        id=row.id,
                        batch_job_url=row.batch_job_url,
                        batch_job_status=row.batch_job_status,
                        callback_url=row.callback_url,
                        caller_id=row.caller_id,
                        metadata_=row.metadata_,
                        webhook_secret=webhook_secret,
                        audio_blob_path=row.audio_blob_path,
                        locale=row.locale,
                        created_datetime=row.created_datetime,
                        processing_started_datetime=row.processing_started_datetime,
                        enable_diarization=row.enable_diarization,
                        trace_parent=row.trace_parent,
                        audio_duration_seconds=row.audio_duration_seconds,
                    )
                )
            return result

    async def _process_job(self, job: _PendingJob) -> None:
        # DIAAT-276: everything emitted while handling this job carries its journey.
        with journey(JOURNEY_JOB, str(job.id), parent_id=upload_id_from_blob(job.audio_blob_path)):
            # The Azure Speech job ID, on every event about this job (DIAAT-282 AC3).
            speech_ref = {c.DEPENDENCY_REF: batch_job_id_from_url(job.batch_job_url)}
            try:
                status_data = await get_batch_job_status(job.batch_job_url)
                azure_status = status_data.get("status", "")

                if azure_status == BatchJobStatus.RUNNING:
                    if job.batch_job_status == BatchJobStatus.NOT_STARTED:
                        # DIAAT-281: processing starts; the queue wait ends here.
                        started = job_telemetry.processing_started_at(
                            status_data, job.created_datetime
                        )
                        await asyncio.to_thread(
                            self._update_status, job.id, BatchJobStatus.RUNNING, started
                        )
                        job_telemetry.record_processing_started(
                            speech_ref, job.created_datetime, started
                        )
                elif azure_status == BatchJobStatus.SUCCEEDED:
                    with stage(
                        "result_processing", attributes=speech_ref, link_from=job.trace_parent
                    ) as processing:
                        await self._handle_succeeded(job, status_data, processing)
                elif azure_status == BatchJobStatus.FAILED:
                    await self._handle_failed(job, status_data)

            except Exception as exc:
                logger.error(
                    "Error polling batch job %s (%s)", job.id, type(exc).__name__, exc_info=exc
                )
                # DIAAT-281: the job stays unfinished and is polled again next interval.
                # DIAAT-282: where it failed (Azure Speech, or our code/database) and
                # what Azure said.
                code = job_telemetry.failure_code(exc, "speech")
                failure = job_telemetry.failure_attributes(code, exc)
                record_event(EVENT_JOB_POLL_FAILED, {**failure, **speech_ref, c.ERROR_CODE: code})

    def _update_status(
        self, job_id: UUID, status: BatchJobStatus, processing_started: datetime | None = None
    ) -> None:
        with Session(get_engine()) as session:
            update_job_batch_status(session, job_id, status, processing_started)

    async def _handle_succeeded(
        self, job: _PendingJob, status_data: dict, processing: StageHandle | None = None
    ) -> None:
        """Azure Speech succeeded: download, post-process and save the transcript.

        DIAAT-283: each step is its own stage of the job journey, nested in
        result_processing (the umbrella for the attempt), so a Speech job that succeeded
        is told apart from a later failure to retrieve, post-process or save its output.
        A post-processing or save failure raises as before (the job is polled again);
        its stage, result_processing and job.poll_failed carry the step's code.
        """
        polled_at = datetime.now(UTC)
        speech_ref = {c.DEPENDENCY_REF: batch_job_id_from_url(job.batch_job_url)}
        failure: tuple[str, BaseException] | None = None
        with stage(STAGE_RESULTS_RETRIEVAL, attributes=speech_ref) as retrieval:
            try:
                dialogue_entries = await get_batch_results(
                    job.batch_job_url, transcription_job_id=job.id
                )
            except BatchResultsInvalidError as exc:
                # Azure's results output could not be parsed: Azure's failure, not ours.
                failure = ("speech.results_invalid", exc)
            except BatchResultError as exc:
                failure = ("speech.results_unavailable", exc)
            except Exception as exc:
                failure = ("speech.results_error", exc)
            if failure is not None:
                retrieval.error(failure[0])
        if failure is not None:
            await self._fail_results(job, status_data, processing, *failure)
            return

        with (
            stage(STAGE_POSTPROCESSING, attributes=speech_ref) as postprocessing,
            job_telemetry.failing_step(
                job_telemetry.CODE_POSTPROCESS_FAILED, stages=(postprocessing, processing)
            ),
        ):
            with speaker_step_report() as speaker_steps:
                processed_entries = process_speakers(dialogue_entries)
            model_identifier = _extract_model_identifier(status_data, job.locale)
            transcription_duration_seconds = _extract_transcription_duration_seconds(
                status_data, job.created_datetime
            )

        # A call to Azure Speech, not post-processing: never raises (DIAAT-243 AC5) and is
        # measured by the Speech call metrics (DIAAT-282).
        model_display_name = await _resolve_model_display_name(model_identifier)
        # DIAAT-286: the configuration that produced the transcript, saved with it.
        provenance = build_provenance(
            locale=job.locale,
            diarization=job.enable_diarization,
            status_data=status_data,
            batch_job_url=job.batch_job_url,
            model_display_name=model_display_name,
            speaker_steps=speaker_steps,
        )

        with (
            stage(STAGE_PERSISTENCE, attributes=speech_ref) as persistence,
            job_telemetry.storage_write("transcript.save", stages=(persistence, processing)),
        ):
            await asyncio.to_thread(
                self._save_results,
                job.id,
                processed_entries,
                BatchJobStatus.SUCCEEDED,
                transcription_duration_seconds,
                model_identifier,
                model_display_name,
                provenance,
            )
        # OBS-5: Azure Speech usage, recorded once the transcript is saved: a save that
        # fails raises before this, and the next poll records it. Only for succeeded jobs
        # (whether Azure bills any audio of a failed job is not known, so none is recorded).
        # A job processed twice (G18) records it twice, like job.finished. Nothing with
        # the mock Speech backend: no audio went to Azure.
        usage = (
            {}
            if self.settings.SPEECH_BACKEND == "mock"
            else job_telemetry.record_speech_usage(
                *job_telemetry.billed_audio_seconds(
                    getattr(dialogue_entries, "audio_duration_seconds", None),
                    job.audio_duration_seconds,
                )
            )
        )
        job_telemetry.record_transcript_available(
            processed_entries, job_telemetry.azure_last_action(status_data), polled_at, speech_ref
        )
        _job_finished(job, status_data, "success", attributes=usage)

        await self._dispatch_success(job, processed_entries)

        try:
            await delete_batch_job(job.batch_job_url)
        except Exception as exc:
            logger.warning("Could not delete batch job for job %s (%s)", job.id, type(exc).__name__)
            with job_telemetry.storage_write("job.mark_needs_cleanup"):
                await asyncio.to_thread(self._record_cleanup_failure, job.id, str(exc))

    async def _fail_results(
        self,
        job: _PendingJob,
        status_data: dict,
        processing: StageHandle | None,
        error_code: str,
        exc: BaseException,
    ) -> None:
        """End the job as failed because its results could not be retrieved.

        The stage is marked failed first (this attempt failed whatever happens next).
        job.finished and its counter come only once the terminal state is stored: if
        storing it fails, the exception propagates, the poll records job.poll_failed and
        the next poll finishes the job, so it is never counted twice.

        Counted as errors (rule: client-caused rejections use fail(), server and
        dependency failures use error()). hmcts.failure.source tells Azure Speech
        failures from ours.
        """
        logger.error("Failed to retrieve batch results for job %s (%s)", job.id, type(exc).__name__)
        if processing is not None:
            processing.error(error_code)
        try:
            with job_telemetry.storage_write("job.mark_failed"):
                await asyncio.to_thread(self._record_error, job.id, str(exc))
        except Exception as db_exc:
            # DIAAT-283: this attempt failed because of the results (the stage says so);
            # the write failure is job.storage_failed. job.poll_failed keeps the
            # attempt's code and source.
            source = job_telemetry.failure_source(error_code, exc)
            job_telemetry.tag_failure(db_exc, error_code, source)
            raise
        _job_finished(job, status_data, "error", error_code, exc=exc)
        await self._dispatch_failure(job, str(exc))

    async def _handle_failed(self, job: _PendingJob, status_data: dict) -> None:
        error_msg = (
            status_data.get("properties", {})
            .get("error", {})
            .get("message", "Azure Batch Transcription failed")
        )
        # The Azure error text is stored on the job (shown in the UI), never logged.
        logger.error("Batch job %s failed in Azure Speech", job.id)
        with job_telemetry.storage_write("job.mark_failed"):
            await asyncio.to_thread(self._record_error, job.id, error_msg)
        # Only once the terminal state is stored: if storing it fails, the job stays
        # RUNNING, the poll records job.poll_failed and the next poll finishes it once.
        _speech_job_failed(job, status_data)

        if job.audio_blob_path:
            try:
                blob_failed = job_telemetry.CODE_BLOB_FAILED
                with job_telemetry.storage_write("blob.mark_failed", blob_failed):
                    async with AsyncAzureBlobManager() as blob_manager:
                        marked = await blob_manager.set_blob_metadata(
                            blob_name=job.audio_blob_path,
                            metadata={
                                "processed": "false",
                                "status": "permanently_failed",
                                "last_error": error_msg[:1000],
                                "failed_at": datetime.now(UTC).isoformat(),
                            },
                        )
                # set_blob_metadata reports its own failures by returning False.
                if marked is False:
                    job_telemetry.record_storage_failure("blob.mark_failed", blob_failed)
            except Exception as exc:
                logger.warning(
                    "Could not mark blob as failed for job %s (%s)", job.id, type(exc).__name__
                )

        await self._dispatch_failure(job, error_msg)

        try:
            await delete_batch_job(job.batch_job_url)
        except Exception as exc:
            logger.warning("Could not delete batch job for job %s (%s)", job.id, type(exc).__name__)
            with job_telemetry.storage_write("job.mark_needs_cleanup"):
                await asyncio.to_thread(self._record_cleanup_failure, job.id, str(exc))

    def _save_results(
        self,
        job_id,
        entries,
        batch_status,
        transcription_duration_seconds=None,
        model_identifier=None,
        model_display_name=None,
        provenance: TranscriptProvenance | None = None,
    ) -> None:
        with Session(get_engine()) as session:
            save_job_results(
                session,
                job_id,
                entries,
                batch_status,
                transcription_duration_seconds=transcription_duration_seconds,
                model_identifier=model_identifier,
                model_display_name=model_display_name,
                provenance=provenance,
            )

    def _record_error(self, job_id, error_msg) -> None:
        with Session(get_engine()) as session:
            mark_job_error(session, job_id, error_msg)

    def _record_cleanup_failure(self, job_id, reason) -> None:
        with Session(get_engine()) as session:
            mark_needs_cleanup(session, job_id, reason)

    def _claim_webhook_dispatch(self, job_id: UUID) -> bool:
        with Session(get_engine()) as session:
            return claim_webhook_dispatch(session, job_id)

    async def _dispatch_success(self, job: _PendingJob, entries) -> None:
        if not job.callback_url:
            return
        if not job.webhook_secret:
            logger.warning(
                "Job %s has callback_url but no webhook_secret; skipping webhook dispatch",
                job.id,
            )
            return
        if not await asyncio.to_thread(self._claim_webhook_dispatch, job.id):
            logger.info(
                "Webhook already dispatched for job %s by another replica; skipping", job.id
            )
            return
        payload = {
            "job_id": str(job.id),
            "status": "succeeded",
            "dialogue_entries": [
                e.model_dump() if hasattr(e, "model_dump") else e for e in entries
            ],
            "error_message": None,
            "metadata": job.metadata_,
        }
        delivered = await dispatch(job.callback_url, job.webhook_secret, payload)
        _record_delivery(delivered, "succeeded")
        if not delivered:
            logger.error(
                "Webhook delivery permanently failed for job %s after all retries; "
                "caller will not receive the success result",
                job.id,
            )

    async def _dispatch_failure(self, job: _PendingJob, error_msg: str) -> None:
        if not job.callback_url:
            return
        if not job.webhook_secret:
            logger.warning(
                "Job %s has callback_url but no webhook_secret; skipping webhook dispatch",
                job.id,
            )
            return
        if not await asyncio.to_thread(self._claim_webhook_dispatch, job.id):
            logger.info(
                "Webhook already dispatched for job %s by another replica; skipping", job.id
            )
            return
        payload = {
            "job_id": str(job.id),
            "status": "failed",
            "dialogue_entries": [],
            "error_message": error_msg,
            "metadata": job.metadata_,
        }
        delivered = await dispatch(job.callback_url, job.webhook_secret, payload)
        _record_delivery(delivered, "failed")
        if not delivered:
            logger.error(
                "Webhook delivery permanently failed for job %s after all retries; "
                "caller will not receive the failure notification",
                job.id,
            )
