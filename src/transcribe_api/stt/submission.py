"""Batch transcription submission pipeline."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from hmcts_otel import capture_trace_context, client_failure_code, journey, record_event, stage
from hmcts_otel import conventions as c
from sqlmodel import Session

from transcribe_api.stt.batch_client import BatchSubmissionError, submit_batch_job
from transcribe_api.stt.speech_auth import SpeechAuthError
from transcribe_api.stt.job_telemetry import record_job_finished, seconds_between
from transcribe_api.stt.speech_telemetry import speech_config_attributes
from transcribe_api.domain.interface_recording import save_job
from transcribe_api.domain.models_recording import BatchJobStatus, JobStatus, SpeechBatchJob
from transcribe_api.runtime.telemetry import (
    EVENT_JOB_CREATED,
    EVENT_JOB_QUEUED,
    JOURNEY_JOB,
    batch_job_id_from_url,
    upload_id_from_blob,
)

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

    When Speech rejects the submission or cannot be authenticated to, the job
    never reached Speech, so its idempotency key is released: a retry with the
    same key creates a new job instead of getting this failed one back. The
    upload blob stays on the failed job (audio_blob_path), as it does for a
    batch job that fails later, because a retry may resubmit the same blob.
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
        # DIAAT-276: the poller links the job's later processing to this request.
        trace_parent=capture_trace_context(),
    )
    job = save_job(session, job)

    with journey(JOURNEY_JOB, str(job.id), parent_id=upload_id_from_blob(audio_blob_path)):
        record_event(EVENT_JOB_CREATED)
        job = await _submit(session, job, audio_url, locale, enable_diarization)
        # DIAAT-281: Azure accepted the job; it now waits for Azure to start it.
        accepted = seconds_between(job.created_datetime, datetime.now(UTC))
        # DIAAT-282: with the configuration reference the job was sent with.
        record_event(
            EVENT_JOB_QUEUED,
            {
                **speech_config_attributes(locale, enable_diarization),
                c.DEPENDENCY_REF: job.batch_job_id,
            },
            duration_ms=accepted * 1000.0 if accepted is not None else None,
        )
        return job


async def _submit(
    session: Session, job: SpeechBatchJob, audio_url: str, locale: str, enable_diarization: bool
) -> SpeechBatchJob:
    config = speech_config_attributes(locale, enable_diarization)
    with stage("speech_submission") as submission:
        try:
            return await _submit_to_speech(session, job, audio_url, locale, enable_diarization, submission)
        except BatchSubmissionError as exc:
            # Azure Speech refused the job: a dependency failure, counted as an error.
            submission.error("speech.submission_rejected")
            record_job_finished(
                "error",
                "speech.submission_rejected",
                created_at=job.created_datetime,
                exc=exc,
                attributes=config,
            )
            raise
        except SpeechAuthError as exc:
            # The pod's workload identity got no token: our configuration, an error.
            submission.error("speech.auth_failed")
            record_job_finished(
                "error",
                "speech.auth_failed",
                created_at=job.created_datetime,
                exc=exc,
                attributes=config,
            )
            raise
        except Exception as exc:
            # Same rule as the stage: a client error is a failure, anything else an error.
            # DIAAT-282: the cause says whether Azure Speech or our code/database failed.
            outcome = "failure" if client_failure_code(exc) else "error"
            if job.batch_job_id:  # Azure accepted the job before we failed
                config[c.DEPENDENCY_REF] = job.batch_job_id
            record_job_finished(
                outcome,
                "job.submission_error",
                created_at=job.created_datetime,
                exc=exc,
                attributes=config,
            )
            raise


async def _submit_to_speech(
    session: Session,
    job: SpeechBatchJob,
    audio_url: str,
    locale: str,
    enable_diarization: bool,
    submission,
) -> SpeechBatchJob:
    try:
        job_url = await submit_batch_job(
            audio_sas_url=audio_url,
            display_name=f"batch-{job.id}",
            locale=locale,
            enable_diarization=enable_diarization,
        )

        batch_job_id = batch_job_id_from_url(job_url)
        # Relates our job ID to the Azure Speech job ID in every later query.
        submission.set_dependency_ref(batch_job_id)

        job.batch_job_id = batch_job_id
        job.batch_job_url = job_url
        job.batch_job_status = BatchJobStatus.NOT_STARTED
        job.status = JobStatus.SUBMITTED
        job = save_job(session, job)

        logger.info("Batch job submitted: job_id=%s azure_job_id=%s", job.id, batch_job_id)

    except BatchSubmissionError as exc:
        # Type and stack only: the Azure error text can echo request content (DIAAT-274 P7).
        logger.error("Batch submission failed for job %s (%s)", job.id, type(exc).__name__, exc_info=exc)
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
        job.status = JobStatus.FAILED
        # Store only the high-level message — full Azure error detail (which
        # may contain endpoint URLs or subscription IDs) goes to Sentry only.
        job.error_message = "Azure batch transcription submission failed"
        job.idempotency_key = None
        job = save_job(session, job)
        raise
    except SpeechAuthError as exc:
        # The pod's workload identity could not get a token (federation or
        # role assignment missing); the detail is in the log and Sentry.
        logger.error("Batch submission failed for job %s (%s)", job.id, type(exc).__name__, exc_info=exc)
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
        job.status = JobStatus.FAILED
        job.error_message = "Could not authenticate to Azure Speech"
        job.idempotency_key = None
        job = save_job(session, job)
        raise
    except Exception as exc:
        # The key is kept: after a timeout, or a failure once Speech accepted
        # the job, Speech may be transcribing, and a same-key retry must not
        # start a second transcription.
        logger.error("Unexpected error submitting job %s (%s)", job.id, type(exc).__name__, exc_info=exc)
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
        job.status = JobStatus.FAILED
        job.error_message = "Job submission failed due to an internal error"
        job = save_job(session, job)
        raise

    return job
