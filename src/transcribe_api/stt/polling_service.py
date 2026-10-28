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

from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager
from transcribe_api.stt.batch_client import (
    BatchResultError,
    delete_batch_job,
    get_batch_job_status,
    get_batch_results,
    get_model_details,
)
from transcribe_api.stt.speakers_recording import process_speakers
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.db_recording import get_engine
from transcribe_api.domain.interface_recording import (
    claim_webhook_dispatch,
    fetch_pending_batch_jobs,
    mark_job_error,
    mark_needs_cleanup,
    save_job_results,
    update_job_batch_status,
)
from transcribe_api.domain.models_recording import BatchJobStatus
from transcribe_api.runtime.telemetry import JOURNEY_JOB, batch_job_id_from_url, upload_id_from_blob
from transcribe_api.domain.webhook import dispatch

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
            start = datetime.fromisoformat(created.replace("Z", "+00:00"))
            end = datetime.fromisoformat(last_action.replace("Z", "+00:00"))
            duration = (end - start).total_seconds()
            if duration >= 0:
                return duration
        except ValueError:
            pass

    if fallback_start is not None:
        return max(0.0, (datetime.now(UTC) - fallback_start).total_seconds())

    return None


def _job_finished_with_error(error_code: str) -> None:
    """job.finished for a job whose FAILED state is stored.

    Emitted only after the write: when it fails, the job is polled again and the next
    poll finishes it, so a job is finished once. These are Azure Speech (dependency)
    failures, so they count as errors (rule: client-caused rejections use fail(),
    server and dependency failures use error()).
    """
    record_event("job.finished", {c.OUTCOME: "error", c.ERROR_CODE: error_code})


class BatchPollingService:
    """Background service that polls Azure Batch Transcription job status."""

    def __init__(self, webhook_secret_resolver=None) -> None:
        self.settings = get_settings()
        self._shutdown = False
        self._resolve_webhook_secret = webhook_secret_resolver or self._default_secret_resolver

    def _default_secret_resolver(self, caller_id: UUID) -> str:
        from transcribe_api.runtime.security import decrypt_webhook_secret
        from transcribe_api.domain.interface_recording import get_caller_by_id

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
        try:
            while not self._shutdown:
                try:
                    await self._poll_once()
                except Exception as exc:
                    logger.error(
                        "Unexpected error in polling loop (%s)", type(exc).__name__, exc_info=exc
                    )
                    sentry_sdk.capture_exception(exc)
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
                        trace_parent=row.trace_parent,
                    )
                )
            return result

    async def _process_job(self, job: _PendingJob) -> None:
        # DIAAT-276: everything emitted while handling this job carries its journey.
        with journey(JOURNEY_JOB, str(job.id), parent_id=upload_id_from_blob(job.audio_blob_path)):
            try:
                status_data = await get_batch_job_status(job.batch_job_url)
                azure_status = status_data.get("status", "")
                speech_ref = {c.DEPENDENCY_REF: batch_job_id_from_url(job.batch_job_url)}

                if azure_status == BatchJobStatus.RUNNING:
                    if job.batch_job_status == BatchJobStatus.NOT_STARTED:
                        await asyncio.to_thread(self._update_status, job.id, BatchJobStatus.RUNNING)
                        record_event("speech.job_running", speech_ref)
                elif azure_status == BatchJobStatus.SUCCEEDED:
                    with stage(
                        "result_processing", attributes=speech_ref, link_from=job.trace_parent
                    ) as processing:
                        await self._handle_succeeded(job, status_data, processing)
                elif azure_status == BatchJobStatus.FAILED:
                    await self._handle_failed(job, status_data, speech_ref)

            except Exception as exc:
                logger.error(
                    "Error polling batch job %s (%s)", job.id, type(exc).__name__, exc_info=exc
                )
                sentry_sdk.capture_exception(exc)

    def _update_status(self, job_id: UUID, status: BatchJobStatus) -> None:
        with Session(get_engine()) as session:
            update_job_batch_status(session, job_id, status)

    async def _handle_succeeded(
        self, job: _PendingJob, status_data: dict, processing: StageHandle | None = None
    ) -> None:
        try:
            dialogue_entries = await get_batch_results(
                job.batch_job_url, transcription_job_id=job.id
            )
        except BatchResultError as exc:
            logger.error(
                "Failed to retrieve batch results for job %s (%s)", job.id, type(exc).__name__
            )
            if processing is not None:
                processing.error("speech.results_unavailable")  # each attempt's code
            await asyncio.to_thread(self._record_error, job.id, str(exc))
            _job_finished_with_error("speech.results_unavailable")
            await self._dispatch_failure(job, str(exc))
            return
        except Exception as exc:
            logger.error(
                "Failed to retrieve batch results for job %s (%s)", job.id, type(exc).__name__
            )
            if processing is not None:
                processing.error("speech.results_error")  # each attempt's code
            await asyncio.to_thread(self._record_error, job.id, str(exc))
            _job_finished_with_error("speech.results_error")
            await self._dispatch_failure(job, str(exc))
            return

        processed_entries = process_speakers(dialogue_entries)

        model_identifier = _extract_model_identifier(status_data, job.locale)
        model_display_name = await _resolve_model_display_name(model_identifier)
        transcription_duration_seconds = _extract_transcription_duration_seconds(
            status_data, job.created_datetime
        )

        await asyncio.to_thread(
            self._save_results,
            job.id,
            processed_entries,
            BatchJobStatus.SUCCEEDED,
            transcription_duration_seconds,
            model_identifier,
            model_display_name,
        )
        record_event("transcript.available")
        record_event("job.finished", {c.OUTCOME: "success"})

        await self._dispatch_success(job, processed_entries)

        try:
            await delete_batch_job(job.batch_job_url)
        except Exception as exc:
            logger.warning("Could not delete batch job for job %s (%s)", job.id, type(exc).__name__)
            await asyncio.to_thread(self._record_cleanup_failure, job.id, str(exc))

    async def _handle_failed(
        self, job: _PendingJob, status_data: dict, speech_ref: dict | None = None
    ) -> None:
        error_msg = (
            status_data.get("properties", {})
            .get("error", {})
            .get("message", "Azure Batch Transcription failed")
        )
        # The Azure error text is stored on the job (shown in the UI), never logged.
        logger.error("Batch job %s failed in Azure Speech", job.id)
        await asyncio.to_thread(self._record_error, job.id, error_msg)
        # After the FAILED state is stored, so a job is finished once. Azure's error
        # text is stored on the job, never exported in telemetry.
        record_event("speech.job_failed", {**(speech_ref or {}), c.ERROR_CODE: "speech.job_failed"})
        _job_finished_with_error("speech.job_failed")

        if job.audio_blob_path:
            try:
                async with AsyncAzureBlobManager() as blob_manager:
                    await blob_manager.set_blob_metadata(
                        blob_name=job.audio_blob_path,
                        metadata={
                            "processed": "false",
                            "status": "permanently_failed",
                            "last_error": error_msg[:1000],
                            "failed_at": datetime.now(UTC).isoformat(),
                        },
                    )
            except Exception as exc:
                logger.warning(
                    "Could not mark blob as failed for job %s (%s)", job.id, type(exc).__name__
                )

        await self._dispatch_failure(job, error_msg)

        try:
            await delete_batch_job(job.batch_job_url)
        except Exception as exc:
            logger.warning("Could not delete batch job for job %s (%s)", job.id, type(exc).__name__)
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
        if not delivered:
            logger.error(
                "Webhook delivery permanently failed for job %s after all retries; "
                "caller will not receive the failure notification",
                job.id,
            )
