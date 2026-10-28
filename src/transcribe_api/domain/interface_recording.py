from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, case, func, or_
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


def update_job_batch_status(
    session: Session,
    job_id: UUID,
    batch_status: BatchJobStatus,
    processing_started: datetime | None = None,
) -> None:
    job = session.get(SpeechBatchJob, job_id)
    if job:
        job.batch_job_status = batch_status
        if batch_status == BatchJobStatus.RUNNING:
            job.status = JobStatus.RUNNING
            if job.processing_started_datetime is None:
                job.processing_started_datetime = processing_started or datetime.now(UTC)
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


# DIAAT-281: a job is waiting until Azure Speech starts processing it (our row exists,
# Azure may or may not have accepted it), running while Azure processes it.
WAITING_STATUSES = (JobStatus.PENDING, JobStatus.SUBMITTED)


@dataclass(frozen=True)
class JobStateCounts:
    waiting: int
    running: int
    # Creation time of the oldest waiting job not yet reported stuck (see count_job_states).
    oldest_waiting_created: datetime | None
    stuck_waiting: int
    stuck_running: int


def _waiting():
    return col(SpeechBatchJob.status).in_(WAITING_STATUSES)


def _running():
    return SpeechBatchJob.status == JobStatus.RUNNING


def _stuck_waiting(cutoff: datetime):
    return and_(_waiting(), col(SpeechBatchJob.created_datetime) < cutoff)


def _stuck_running(cutoff: datetime):
    # Running jobs without a processing start (rows from before revision 0003) are
    # measured from their creation.
    running_since = func.coalesce(
        SpeechBatchJob.processing_started_datetime, SpeechBatchJob.created_datetime
    )
    return and_(_running(), running_since < cutoff)


def _waiting_unreported():
    # A job already reported stuck may never finish (e.g. a PENDING orphan that is never
    # polled); it stays in the stuck counts until someone clears it, but must not hold
    # the oldest-waiting age up for good.
    return and_(_waiting(), col(SpeechBatchJob.stuck_reported_at).is_(None))


def _count(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def count_job_states(
    session: Session, *, waiting_cutoff: datetime, running_cutoff: datetime
) -> JobStateCounts:
    """Database-wide waiting/running/stuck counts and the oldest waiting job, in one query.

    The waiting, running and stuck counts include every job in those states, including
    jobs already reported stuck (they really are stuck until someone clears them). The
    oldest waiting job excludes jobs already reported stuck (stuck_reported_at set): they
    are covered by the stuck counts and ``job.stuck``, and one that can never finish
    would otherwise keep the oldest-waiting age, and its early-warning alert, high for
    good, hiding a real backlog behind it.
    """
    stmt = select(
        _count(_waiting()),
        _count(_running()),
        func.min(case((_waiting_unreported(), SpeechBatchJob.created_datetime), else_=None)),
        _count(_stuck_waiting(waiting_cutoff)),
        _count(_stuck_running(running_cutoff)),
    ).where(col(SpeechBatchJob.status).in_([*WAITING_STATUSES, JobStatus.RUNNING]))
    row = session.execute(stmt).one()
    return JobStateCounts(int(row[0]), int(row[1]), row[2], int(row[3]), int(row[4]))


@dataclass(frozen=True)
class StuckJob:
    id: UUID
    status: JobStatus
    audio_blob_path: str | None
    batch_job_id: str | None
    created_datetime: datetime
    processing_started_datetime: datetime | None


def claim_stuck_jobs(
    session: Session, *, waiting_cutoff: datetime, running_cutoff: datetime
) -> list[StuckJob]:
    """Mark newly stuck jobs as reported and return them. Each job is returned once.

    A conditional UPDATE (stuck_reported_at IS NULL), like claim_webhook_dispatch, so
    only one replica reports a given job even when several scan at the same time.
    """
    stmt = (
        sa_update(SpeechBatchJob)
        .where(
            col(SpeechBatchJob.stuck_reported_at).is_(None),
            or_(_stuck_waiting(waiting_cutoff), _stuck_running(running_cutoff)),
        )
        .values(stuck_reported_at=datetime.now(UTC))
        .returning(
            SpeechBatchJob.id,
            SpeechBatchJob.status,
            SpeechBatchJob.audio_blob_path,
            SpeechBatchJob.batch_job_id,
            SpeechBatchJob.created_datetime,
            SpeechBatchJob.processing_started_datetime,
        )
    )
    rows = session.execute(stmt).all()
    session.commit()
    return [StuckJob(*row) for row in rows]


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
