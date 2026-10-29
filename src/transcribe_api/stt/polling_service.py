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
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID

import sentry_sdk
from hmcts_otel import StageHandle, journey, record_event, stage
from hmcts_otel import conventions as c
from sqlmodel import Session

from transcribe_api.domain.interface_recording import (
    claim_webhook_dispatch,
    fetch_pending_batch_jobs,
    mark_job_error,
    mark_needs_cleanup,
    save_job_results,
    update_job_batch_status,
)
from transcribe_api.domain.models_recording import BatchJobStatus
from transcribe_api.domain.webhook import dispatch
from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager
from transcribe_api.runtime.db_recording import get_engine
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import (
    ATTR_FAILURE_SOURCE,
    ATTR_SPEECH_ERROR_CODE,
    ATTR_WEBHOOK_NOTIFICATION,
    EVENT_JOB_FAILED_IN_SPEECH,
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
from transcribe_api.stt.speakers_recording import process_speakers
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


class BatchPollingService:
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
                    job_telemetry.poller_heartbeat.succeeded()
                except Exception as exc:
                    logger.error(
                        "Unexpected error in polling loop (%s)", type(exc).__name__, exc_info=exc
                    )
                    sentry_sdk.capture_exception(exc)
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
                                "Cannot resolve webhook secret for caller %s (job %s skipped): %s",
                                row.caller_id,
                                row.id,
                                exc,
                            )
                            sentry_sdk.capture_exception(exc)
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
                sentry_sdk.capture_exception(exc)
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
            processed_entries = process_speakers(dialogue_entries)
            model_identifier = _extract_model_identifier(status_data, job.locale)
            transcription_duration_seconds = _extract_transcription_duration_seconds(
                status_data, job.created_datetime
            )

        # A call to Azure Speech, not post-processing: never raises (DIAAT-243 AC5) and is
        # measured by the Speech call metrics (DIAAT-282).
        model_display_name = await _resolve_model_display_name(model_identifier)

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
            )
        job_telemetry.record_transcript_available(
            processed_entries, job_telemetry.azure_last_action(status_data), polled_at, speech_ref
        )
        _job_finished(job, status_data, "success")

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
