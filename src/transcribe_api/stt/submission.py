"""Batch transcription submission pipeline."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlmodel import Session

from transcribe_api.stt.batch_client import BatchSubmissionError, submit_batch_job
from transcribe_api.domain.interface_recording import save_job
from transcribe_api.domain.models_recording import BatchJobStatus, JobStatus, SpeechBatchJob

logger = logging.getLogger(__name__)


async def submit_and_queue_batch_job(
    session: Session,
    audio_url: str,
    caller_id: UUID | None = None,
    locale: str = "en-GB",
    enable_diarization: bool = True,
    callback_url: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
    audio_duration_seconds: float | None = None,
    audio_blob_path: str | None = None,
    user_id: UUID | None = None,
) -> SpeechBatchJob:
    """Submit audio to Azure Batch Transcription and persist the initial job record.

    Creates a SpeechBatchJob with status PENDING, submits to Azure,
    then updates the record to SUBMITTED. On failure, saves error state.
    """
    job = SpeechBatchJob(
        caller_id=caller_id,
        user_id=user_id,
        audio_url=audio_url,
        locale=locale,
        enable_diarization=enable_diarization,
        callback_url=callback_url,
        idempotency_key=idempotency_key,
        metadata_=metadata or {},
        audio_duration_seconds=audio_duration_seconds,
        audio_blob_path=audio_blob_path,
        status=JobStatus.PENDING,
    )
    job = save_job(session, job)

    try:
        job_url = await submit_batch_job(
            audio_sas_url=audio_url,
            display_name=f"batch-{job.id}",
            locale=locale,
            enable_diarization=enable_diarization,
        )

        batch_job_id = job_url.rstrip("/").split("/")[-1]

        job.batch_job_id = batch_job_id
        job.batch_job_url = job_url
        job.batch_job_status = BatchJobStatus.NOT_STARTED
        job.status = JobStatus.SUBMITTED
        job = save_job(session, job)

        logger.info("Batch job submitted: job_id=%s azure_job_id=%s", job.id, batch_job_id)

    except BatchSubmissionError as exc:
        logger.error("Batch submission failed for job %s: %s", job.id, exc)
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
        job.status = JobStatus.FAILED
        # Store only the high-level message — full Azure error detail (which
        # may contain endpoint URLs or subscription IDs) goes to Sentry only.
        job.error_message = "Azure batch transcription submission failed"
        job = save_job(session, job)
        raise
    except Exception as exc:
        logger.error("Unexpected error submitting job %s: %s", job.id, exc)
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
        job.status = JobStatus.FAILED
        job.error_message = "Job submission failed due to an internal error"
        job = save_job(session, job)
        raise

    return job
