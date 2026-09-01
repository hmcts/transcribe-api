from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy import update as sa_update
from sqlmodel import Session, col, select

from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.domain.models_recording import (
    BatchJobStatus,
    Caller,
    CorrectionDatasetEntry,
    DialogueEntry,
    JobStatus,
    SpeechBatchJob,
)

_POLL_BATCH_SIZE = 10


def save_job(session: Session, job: SpeechBatchJob) -> SpeechBatchJob:
    job.updated_datetime = datetime.now(UTC)
    merged = session.merge(job)
    session.commit()
    session.refresh(merged)
    return merged


def get_job_by_id(session: Session, job_id: UUID) -> SpeechBatchJob | None:
    return session.get(SpeechBatchJob, job_id)


def list_jobs_paginated(
    session: Session,
    user_id: UUID | None = None,
    status: JobStatus | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[SpeechBatchJob], int]:
    """List jobs with optional user_id filter.

    Pass user_id=None to return all jobs regardless of owner (admin use).
    Pass a specific user_id to return only that user's jobs.
    """
    conditions = []
    if user_id is not None:
        conditions.append(SpeechBatchJob.user_id == user_id)
    if status is not None:
        conditions.append(SpeechBatchJob.status == status)

    count_stmt = select(func.count(SpeechBatchJob.id))
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total: int = session.execute(count_stmt).scalar_one()

    jobs_stmt = (
        select(SpeechBatchJob)
        .order_by(col(SpeechBatchJob.created_datetime).desc())
        .limit(limit)
        .offset(offset)
    )
    if conditions:
        jobs_stmt = jobs_stmt.where(*conditions)
    return list(session.exec(jobs_stmt).all()), total


def get_job_by_idempotency_key(
    session: Session, key: str, caller_id: UUID
) -> SpeechBatchJob | None:
    stmt = select(SpeechBatchJob).where(
        SpeechBatchJob.idempotency_key == key,
        SpeechBatchJob.caller_id == caller_id,
    )
    return session.exec(stmt).first()


def get_job_by_idempotency_key_for_user(
    session: Session, key: str, user_id: UUID
) -> SpeechBatchJob | None:
    """Idempotency lookup for JWT-authenticated callers (keyed by user_id)."""
    stmt = select(SpeechBatchJob).where(
        SpeechBatchJob.idempotency_key == key,
        SpeechBatchJob.user_id == user_id,
    )
    return session.exec(stmt).first()


def fetch_pending_batch_jobs(session: Session) -> list[SpeechBatchJob]:
    stmt = (
        select(SpeechBatchJob)
        .where(
            col(SpeechBatchJob.batch_job_status).in_(
                [BatchJobStatus.NOT_STARTED, BatchJobStatus.RUNNING]
            ),
            SpeechBatchJob.status != JobStatus.FAILED,
        )
        .with_for_update(skip_locked=True)
        .limit(_POLL_BATCH_SIZE)
    )
    return list(session.exec(stmt).all())


def update_job_batch_status(session: Session, job_id: UUID, batch_status: BatchJobStatus) -> None:
    job = session.get(SpeechBatchJob, job_id)
    if job:
        job.batch_job_status = batch_status
        if batch_status == BatchJobStatus.RUNNING:
            job.status = JobStatus.RUNNING
        job.updated_datetime = datetime.now(UTC)
        session.add(job)
        session.commit()


def save_job_results(
    session: Session,
    job_id: UUID,
    entries: list[DialogueEntry],
    batch_status: BatchJobStatus,
    transcription_duration_seconds: float | None = None,
    model_identifier: str | None = None,
    model_display_name: str | None = None,
) -> None:
    job = session.get(SpeechBatchJob, job_id)
    if job:
        job.dialogue_entries = [e.model_dump() if hasattr(e, "model_dump") else e for e in entries]
        job.batch_job_status = batch_status
        job.status = (
            JobStatus.SUCCEEDED if batch_status == BatchJobStatus.SUCCEEDED else JobStatus.FAILED
        )
        job.transcription_duration_seconds = transcription_duration_seconds
        job.model_identifier = model_identifier
        job.model_display_name = model_display_name
        job.updated_datetime = datetime.now(UTC)
        session.add(job)
        session.commit()


def mark_job_error(
    session: Session,
    job_id: UUID,
    error_message: str,
    batch_status: BatchJobStatus = BatchJobStatus.FAILED,
) -> None:
    job = session.get(SpeechBatchJob, job_id)
    if job:
        job.batch_job_status = batch_status
        job.status = JobStatus.FAILED
        job.error_message = error_message
        job.updated_datetime = datetime.now(UTC)
        session.add(job)
        session.commit()


def mark_needs_cleanup(session: Session, job_id: UUID, reason: str) -> None:
    job = session.get(SpeechBatchJob, job_id)
    if job:
        job.needs_cleanup = True
        job.cleanup_failure_reason = reason[:500]
        job.updated_datetime = datetime.now(UTC)
        session.add(job)
        session.commit()


def claim_webhook_dispatch(session: Session, job_id: UUID) -> bool:
    """Atomically mark a job's webhook as dispatched. Returns True only for the first caller.

    Uses a conditional UPDATE (WHERE webhook_dispatched_at IS NULL) so only one
    replica wins the race even if multiple pick up the same job simultaneously.
    """
    stmt = (
        sa_update(SpeechBatchJob)
        .where(
            SpeechBatchJob.id == job_id,
            SpeechBatchJob.webhook_dispatched_at.is_(None),
        )
        .values(webhook_dispatched_at=datetime.now(UTC))
        .returning(SpeechBatchJob.id)
    )
    result = session.execute(stmt)
    session.commit()
    return result.first() is not None


def record_correction_dataset_entry(
    session: Session,
    *,
    job: SpeechBatchJob,
    segment_index: int,
    correction_kind: str,
    original_text: str,
    corrected_text: str,
    confidence: float | None,
    speaker: str,
    start_word_index: int | None = None,
    end_word_index: int | None = None,
) -> None:
    """Stage a row for the corrections training dataset (DIAAT-231).

    A no-op unless `Settings.CORRECTIONS_DATASET_EXPORT_ENABLED` is True —
    see CorrectionDatasetEntry's docstring for why this is off by default
    (retention/anonymisation sign-off for real hearing content is pending).

    Only stages the row via `session.add` — it's the caller's responsibility
    to commit (typically alongside the job update it accompanies), so the
    correction and its dataset copy are persisted atomically.
    """
    if not get_settings().CORRECTIONS_DATASET_EXPORT_ENABLED:
        return

    session.add(
        CorrectionDatasetEntry(
            job_id=job.id,
            caller_id=job.caller_id,
            segment_index=segment_index,
            correction_kind=correction_kind,
            start_word_index=start_word_index,
            end_word_index=end_word_index,
            speaker=speaker,
            locale=job.locale,
            original_text=original_text,
            corrected_text=corrected_text,
            confidence=confidence,
        )
    )


def get_caller_by_id(session: Session, caller_id: UUID) -> Caller | None:
    return session.get(Caller, caller_id)


def get_caller_by_lookup_hash(session: Session, lookup_hash: str) -> Caller | None:
    stmt = select(Caller).where(
        Caller.key_lookup_hash == lookup_hash,
        Caller.is_active == True,  # noqa: E712
    )
    return session.exec(stmt).first()


def get_all_active_callers(session: Session) -> list[Caller]:
    stmt = select(Caller).where(Caller.is_active == True)  # noqa: E712
    return list(session.exec(stmt).all())
