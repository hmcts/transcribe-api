from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, case, exists, func, or_
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlmodel import Session, col, select

from transcribe_api.domain.models_recording import (
    BatchJobStatus,
    Caller,
    CorrectionDatasetEntry,
    DialogueEntry,
    JobStatus,
    SpeechBatchJob,
    TranscriptionJobPart,
)
from transcribe_api.runtime.settings_recording import get_settings

if TYPE_CHECKING:
    from transcribe_api.domain.provenance_recording import TranscriptProvenance

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
    user_id: UUID,
    status: JobStatus | None = None,
    limit: int = 20,
    offset: int = 0,
    *,
    jurisdiction_id: UUID,
) -> tuple[list[SpeechBatchJob], int]:
    """List one user's jobs in one jurisdiction (DIAAT-334). The owner is required: nobody
    lists another's jobs or the ownerless pre-migration ones (DIAAT-389, DIAAT-397)."""
    conditions = [SpeechBatchJob.jurisdiction_id == jurisdiction_id, SpeechBatchJob.user_id == user_id]
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
    provenance: TranscriptProvenance | None = None,
) -> None:
    """Save the transcript and, in the same transaction, its provenance (DIAAT-286).

    The provenance's generated_at is the time the results are saved.
    """
    job = session.get(SpeechBatchJob, job_id)
    if job:
        now = datetime.now(UTC)
        job.dialogue_entries = [e.model_dump() if hasattr(e, "model_dump") else e for e in entries]
        job.batch_job_status = batch_status
        job.status = (
            JobStatus.SUCCEEDED if batch_status == BatchJobStatus.SUCCEEDED else JobStatus.FAILED
        )
        job.transcription_duration_seconds = transcription_duration_seconds
        job.model_identifier = model_identifier
        job.model_display_name = model_display_name
        if provenance is not None:
            job.provenance = provenance.model_copy(update={"generated_at": now}).model_dump(
                mode="json"
            )
        job.updated_datetime = now
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


def _has_parts():
    return exists().where(TranscriptionJobPart.job_id == SpeechBatchJob.id)


def _stuck_running(cutoff: datetime):
    # Running jobs without a processing start (rows from before revision 0003) are
    # measured from their creation. DIAAT-408: a job in parts is measured against its longest
    # part instead (multipart_stuck_running), not against the single-file threshold.
    running_since = func.coalesce(
        SpeechBatchJob.processing_started_datetime, SpeechBatchJob.created_datetime
    )
    return and_(_running(), ~_has_parts(), running_since < cutoff)


@dataclass(frozen=True)
class RunningMultipartJob:
    id: UUID
    running_since: datetime
    longest_part_seconds: float
    reported: bool


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def multipart_stuck_running(session: Session, now: datetime) -> list[RunningMultipartJob]:
    """DIAAT-408: the RUNNING jobs in parts that have run longer than ``JOB_STUCK_PART_FACTOR`` (1.5)
    times their longest part. The parts run in parallel, so a job takes about as long as its longest
    part; the single-file threshold (a fixed 6 hours) would be far too slow for a 2-hour part and is
    not applied to a job in parts. Reported or not: the caller picks."""
    factor = get_settings().JOB_STUCK_PART_FACTOR
    rows = session.execute(
        select(
            SpeechBatchJob.id,
            func.coalesce(SpeechBatchJob.processing_started_datetime, SpeechBatchJob.created_datetime),
            func.max(TranscriptionJobPart.duration_seconds),
            SpeechBatchJob.stuck_reported_at,
        )
        .join(TranscriptionJobPart, TranscriptionJobPart.job_id == SpeechBatchJob.id)
        .where(_running())
        .group_by(SpeechBatchJob.id)
    ).all()
    stuck = []
    for job_id, since, longest, reported_at in rows:
        if since is None or longest is None:
            continue
        since = _as_utc(since)
        if (now - since).total_seconds() > factor * float(longest):
            stuck.append(RunningMultipartJob(job_id, since, float(longest), reported_at is not None))
    return stuck


def _waiting_unreported():
    # A job already reported stuck may never finish (e.g. a PENDING orphan that is never
    # polled); it stays in the stuck counts until someone clears it, but must not hold
    # the oldest-waiting age up for good.
    return and_(_waiting(), col(SpeechBatchJob.stuck_reported_at).is_(None))


def _count(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def count_job_states(
    session: Session, *, waiting_cutoff: datetime, running_cutoff: datetime, now: datetime | None = None
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
    stuck_in_parts = len(multipart_stuck_running(session, now or datetime.now(UTC)))
    return JobStateCounts(int(row[0]), int(row[1]), row[2], int(row[3]), int(row[4]) + stuck_in_parts)


@dataclass(frozen=True)
class StuckJob:
    id: UUID
    status: JobStatus
    audio_blob_path: str | None
    batch_job_id: str | None
    created_datetime: datetime
    processing_started_datetime: datetime | None


def claim_stuck_jobs(
    session: Session, *, waiting_cutoff: datetime, running_cutoff: datetime, now: datetime | None = None
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
    claimed = [StuckJob(*row) for row in rows]
    # DIAAT-408: jobs in parts are stuck against their longest part, claimed the same way.
    for job in multipart_stuck_running(session, now or datetime.now(UTC)):
        if job.reported:
            continue
        done = session.execute(
            sa_update(SpeechBatchJob)
            .where(SpeechBatchJob.id == job.id, col(SpeechBatchJob.stuck_reported_at).is_(None))
            .values(stuck_reported_at=datetime.now(UTC))
            .returning(
                SpeechBatchJob.id,
                SpeechBatchJob.status,
                SpeechBatchJob.audio_blob_path,
                SpeechBatchJob.batch_job_id,
                SpeechBatchJob.created_datetime,
                SpeechBatchJob.processing_started_datetime,
            )
        ).first()
        session.commit()
        if done is not None:
            claimed.append(StuckJob(*done))
    return claimed


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
    claimed = session.execute(stmt).first() is not None  # read before the commit: SQLite refuses otherwise
    session.commit()
    return claimed


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


# DIAAT-408: jobs whose audio is prepared by the worker (stt/preparation_service.py) -------------
#
# A job with ``source_blob_path`` set, ``PENDING`` and with no Azure job yet is waiting for
# preparation. Claiming one adds 1 to ``preparation_attempts`` and stamps ``updated_datetime``
# (the lease). While the worker prepares it the stamp is renewed; a job with attempts is claimed
# again only once the stamp is older than the lease.


def _awaiting_preparation():
    return and_(
        SpeechBatchJob.status == JobStatus.PENDING,
        col(SpeechBatchJob.source_blob_path).is_not(None),
        col(SpeechBatchJob.batch_job_id).is_(None),
    )


def _lease_free(cutoff: datetime):
    """Never claimed, or claimed and not renewed since ``cutoff``."""
    return or_(
        SpeechBatchJob.preparation_attempts == 0,
        col(SpeechBatchJob.updated_datetime) <= cutoff,
    )


def claim_job_for_preparation(
    session: Session, *, max_attempts: int, lease_seconds: float, now: datetime | None = None
) -> SpeechBatchJob | None:
    """Claim the oldest job waiting for preparation whose lease is free and whose attempts are not
    used up, or None. One conditional UPDATE per candidate (compare-and-swap on the attempt count),
    so two workers never claim the same job. The job is returned with the attempt just started in
    ``preparation_attempts``."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=lease_seconds)
    candidates = session.exec(
        select(SpeechBatchJob.id, SpeechBatchJob.preparation_attempts)
        .where(_awaiting_preparation(), SpeechBatchJob.preparation_attempts < max_attempts, _lease_free(cutoff))
        .order_by(col(SpeechBatchJob.created_datetime))
        .limit(_POLL_BATCH_SIZE)
    ).all()
    for job_id, attempts in candidates:
        claimed = session.execute(
            sa_update(SpeechBatchJob)
            .where(
                SpeechBatchJob.id == job_id,
                _awaiting_preparation(),
                SpeechBatchJob.preparation_attempts == attempts,
                _lease_free(cutoff),
            )
            .values(preparation_attempts=attempts + 1, updated_datetime=now)
        )
        session.commit()
        if claimed.rowcount == 1:
            session.expire_all()
            return session.get(SpeechBatchJob, job_id)
    return None


def renew_preparation(session: Session, job_id: UUID) -> bool:
    """Stamp a job being prepared as alive, so its lease does not run out. False if it is no longer
    waiting for preparation (deleted, or already submitted)."""
    renewed = session.execute(
        sa_update(SpeechBatchJob)
        .where(SpeechBatchJob.id == job_id, _awaiting_preparation())
        .values(updated_datetime=datetime.now(UTC))
    )
    session.commit()
    return renewed.rowcount == 1


def release_preparation(session: Session, job_id: UUID, *, retry_after_seconds: float, lease_seconds: float) -> None:
    """Give a job back after a failed attempt: it is claimed again ``retry_after_seconds`` from now
    (its lease is made to run out then; the attempt stays counted)."""
    stamp = datetime.now(UTC) - timedelta(seconds=lease_seconds) + timedelta(seconds=retry_after_seconds)
    session.execute(
        sa_update(SpeechBatchJob)
        .where(SpeechBatchJob.id == job_id, _awaiting_preparation())
        .values(updated_datetime=stamp)
    )
    session.commit()


def mark_preparation_failed(session: Session, job_id: UUID, message: str) -> bool:
    """End a job that could not be prepared: FAILED with ``message``, its idempotency key released
    (as for a submission Azure Speech refused: a retry with the key makes a new job). False if the
    job is gone or is no longer waiting for preparation."""
    job = session.get(SpeechBatchJob, job_id)
    if job is None or job.status != JobStatus.PENDING or job.batch_job_id is not None:
        return False
    job.status = JobStatus.FAILED
    job.error_message = message
    job.idempotency_key = None
    job.updated_datetime = datetime.now(UTC)
    session.add(job)
    session.commit()
    return True


@dataclass(frozen=True)
class FailedPreparation:
    """A job ``fail_exhausted_preparations`` ended, as it was (its row has since changed)."""

    id: UUID
    audio_blob_path: str | None
    created_datetime: datetime
    preparation_attempts: int


def fail_exhausted_preparations(
    session: Session, *, max_attempts: int, lease_seconds: float, message: str, now: datetime | None = None
) -> list[FailedPreparation]:
    """Fail the jobs that used up their attempts without being prepared and are not being worked on
    (their lease has run out): the last attempt crashed. Returns them, once each."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=lease_seconds)
    rows = session.exec(
        select(SpeechBatchJob)
        .where(
            _awaiting_preparation(),
            SpeechBatchJob.preparation_attempts >= max_attempts,
            col(SpeechBatchJob.updated_datetime) <= cutoff,
        )
        .limit(_POLL_BATCH_SIZE)
    ).all()
    seen = [
        FailedPreparation(row.id, row.audio_blob_path, row.created_datetime, row.preparation_attempts) for row in rows
    ]
    return [job for job in seen if mark_preparation_failed(session, job.id, message)]


# DIAAT-408: the parts of a recording over 240 minutes (stt/preparation_service.py) ---------------


@dataclass(frozen=True)
class PartPlan:
    """A part as the worker has cut it, to be recorded."""

    part_index: int
    part_count: int
    offset_seconds: float
    duration_seconds: float
    cut_kind: str
    blob_path: str


def get_job_parts(session: Session, job_id: UUID) -> list[TranscriptionJobPart]:
    """The job's parts, in order (empty for a job of 240 minutes or less)."""
    return list(
        session.exec(
            select(TranscriptionJobPart)
            .where(TranscriptionJobPart.job_id == job_id)
            .order_by(col(TranscriptionJobPart.part_index))
        ).all()
    )


def create_job_parts(session: Session, job_id: UUID, plans: list[PartPlan]) -> list[TranscriptionJobPart]:
    """Record the job's parts (one transaction). If it already has parts (an attempt that was
    interrupted after recording them) they are kept and returned: the plan the first attempt made is
    the one the parts already submitted belong to."""
    existing = get_job_parts(session, job_id)
    if existing:
        return existing
    now = datetime.now(UTC)
    for plan in plans:
        session.add(
            TranscriptionJobPart(
                job_id=job_id,
                part_index=plan.part_index,
                part_count=plan.part_count,
                offset_seconds=plan.offset_seconds,
                duration_seconds=plan.duration_seconds,
                cut_kind=plan.cut_kind,
                blob_path=plan.blob_path,
                updated_datetime=now,
            )
        )
    session.commit()
    return get_job_parts(session, job_id)


def delete_job_parts(session: Session, job_id: UUID) -> None:
    """Remove the job's part rows (the database also does it when the job row goes)."""
    session.execute(sa_delete(TranscriptionJobPart).where(col(TranscriptionJobPart.job_id) == job_id))
    session.commit()


def start_part_attempt(session: Session, part_id: UUID) -> int | None:
    """Count another submission attempt of a part that has not been submitted; returns the attempt
    number, or None if the part is gone (its job was deleted) or is already submitted."""
    stmt = (
        sa_update(TranscriptionJobPart)
        .where(TranscriptionJobPart.id == part_id, col(TranscriptionJobPart.batch_job_id).is_(None))
        .values(attempts=TranscriptionJobPart.attempts + 1, updated_datetime=datetime.now(UTC))
        .returning(TranscriptionJobPart.attempts)
    )
    row = session.execute(stmt).first()
    session.commit()
    return None if row is None else int(row[0])


def mark_part_submitted(session: Session, part_id: UUID, batch_job_id: str, batch_job_url: str) -> bool:
    """Record the Azure job of a part. False if the part is gone (the job was deleted meanwhile)."""
    stmt = (
        sa_update(TranscriptionJobPart)
        .where(TranscriptionJobPart.id == part_id)
        .values(
            batch_job_id=batch_job_id,
            batch_job_url=batch_job_url,
            batch_job_status=BatchJobStatus.NOT_STARTED.value,
            status=JobStatus.SUBMITTED.value,
            error_message=None,
            updated_datetime=datetime.now(UTC),
        )
    )
    done = session.execute(stmt).rowcount == 1
    session.commit()
    return done


def mark_part_failed(session: Session, part_id: UUID, message: str) -> None:
    stmt = (
        sa_update(TranscriptionJobPart)
        .where(TranscriptionJobPart.id == part_id)
        .values(status=JobStatus.FAILED.value, error_message=message[:500], updated_datetime=datetime.now(UTC))
    )
    session.execute(stmt)
    session.commit()


def clear_part_resources(session: Session, job_id: UUID) -> None:
    """The part blobs and Azure jobs of the job have been deleted: forget where they were (the rows
    stay as the record of the parts)."""
    session.execute(
        sa_update(TranscriptionJobPart)
        .where(col(TranscriptionJobPart.job_id) == job_id)
        .values(blob_path=None, batch_job_url=None, updated_datetime=datetime.now(UTC))
    )
    session.commit()


def clear_needs_cleanup(session: Session, job_id: UUID) -> None:
    """The job's leftovers have been deleted after all: forget the failure that was recorded."""
    session.execute(
        sa_update(SpeechBatchJob)
        .where(SpeechBatchJob.id == job_id, SpeechBatchJob.needs_cleanup.is_(True))  # type: ignore[attr-defined]
        .values(needs_cleanup=False, cleanup_failure_reason=None)
    )
    session.commit()


def settle_parts_submitted(session: Session, job_id: UUID) -> bool:
    """Every part of the job is with Azure Speech: the job is SUBMITTED. Only a job still waiting for
    preparation changes (False if it is gone or was settled already)."""
    job = session.get(SpeechBatchJob, job_id)
    if job is None or job.status != JobStatus.PENDING or job.batch_job_id is not None:
        return False
    job.status = JobStatus.SUBMITTED
    job.updated_datetime = datetime.now(UTC)
    session.add(job)
    session.commit()
    return True


# DIAAT-408: polling and assembling a recording in parts (stt/polling_service.py) ------------------
#
# A job in parts has no Azure job of its own: it is selected through its part rows. Every write that
# moves the job on is conditional on the job still being SUBMITTED or RUNNING, so a part result that
# arrives after the job failed (or was completed) changes nothing.

_IN_FLIGHT_JOB = (JobStatus.SUBMITTED, JobStatus.RUNNING)
_PART_IN_FLIGHT = (JobStatus.SUBMITTED.value, JobStatus.RUNNING.value)


def fetch_pending_parts(session: Session, limit: int = _POLL_BATCH_SIZE) -> list[TranscriptionJobPart]:
    """The parts to poll: submitted or running parts of jobs still in flight, the one polled longest
    ago first (every poll stamps ``updated_datetime``), so many jobs in parts share the polling
    fairly and none starves the single-file jobs, which are fetched by their own query. Locked with
    SKIP LOCKED, as fetch_pending_batch_jobs does, so two pollers take different parts."""
    in_flight_jobs = select(SpeechBatchJob.id).where(col(SpeechBatchJob.status).in_(_IN_FLIGHT_JOB))
    stmt = (
        select(TranscriptionJobPart)
        .where(
            col(TranscriptionJobPart.status).in_(_PART_IN_FLIGHT),
            col(TranscriptionJobPart.batch_job_url).is_not(None),
            col(TranscriptionJobPart.job_id).in_(in_flight_jobs),
        )
        .order_by(col(TranscriptionJobPart.updated_datetime).asc().nulls_first())
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def record_part_polled(
    session: Session, part_id: UUID, *, status: str | None = None, batch_status: BatchJobStatus | None = None
) -> bool:
    """Stamp a part as polled, and record its new state if it has one. Only a part of a job still in
    flight changes (False otherwise: the job has failed, finished or gone, and a late answer is
    ignored)."""
    values: dict[str, object] = {"updated_datetime": datetime.now(UTC)}
    if status is not None:
        values["status"] = status
    if batch_status is not None:
        values["batch_job_status"] = batch_status.value
    in_flight_jobs = select(SpeechBatchJob.id).where(col(SpeechBatchJob.status).in_(_IN_FLIGHT_JOB))
    changed = session.execute(
        sa_update(TranscriptionJobPart)
        .where(TranscriptionJobPart.id == part_id, col(TranscriptionJobPart.job_id).in_(in_flight_jobs))
        .values(**values)
    )
    session.commit()
    return changed.rowcount == 1


def mark_job_running(session: Session, job_id: UUID, started: datetime) -> bool:
    """The first part has started processing: SUBMITTED -> RUNNING, with the processing start (the
    end of the queue wait). True only for the call that made the change."""
    changed = session.execute(
        sa_update(SpeechBatchJob)
        .where(SpeechBatchJob.id == job_id, SpeechBatchJob.status == JobStatus.SUBMITTED)
        .values(status=JobStatus.RUNNING, processing_started_datetime=started, updated_datetime=datetime.now(UTC))
    )
    session.commit()
    return changed.rowcount == 1


def fetch_jobs_ready_to_assemble(session: Session, limit: int = _POLL_BATCH_SIZE) -> list[UUID]:
    """Jobs in flight whose parts have all succeeded: ready to be assembled. Selected by state, not by
    the poll that saw the last part succeed, so a poller that died between the two is picked up again."""
    not_succeeded = exists().where(
        TranscriptionJobPart.job_id == SpeechBatchJob.id,
        TranscriptionJobPart.status != JobStatus.SUCCEEDED.value,
    )
    stmt = (
        select(SpeechBatchJob.id)
        .where(col(SpeechBatchJob.status).in_(_IN_FLIGHT_JOB), _has_parts(), ~not_succeeded)
        .order_by(col(SpeechBatchJob.updated_datetime).asc().nulls_first())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def complete_multipart_job(
    session: Session,
    job_id: UUID,
    entries: list[DialogueEntry],
    *,
    transcription_duration_seconds: float | None,
    model_identifier: str | None,
    model_display_name: str | None,
    provenance: TranscriptProvenance | None,
) -> bool:
    """Save the assembled transcript and finish the job, in **one** UPDATE: either the whole
    transcript is there with the job SUCCEEDED or nothing changed, so no half-assembled transcript is
    ever visible. Conditional on the job still being SUBMITTED or RUNNING: when two pollers assemble
    the same job (or the worker restarts and assembles it again) exactly one UPDATE changes a row and
    only that caller returns True; the others did the same work for nothing and must do nothing more
    (no telemetry, no webhook, no cleanup)."""
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "dialogue_entries": [e.model_dump() if hasattr(e, "model_dump") else e for e in entries],
        "batch_job_status": BatchJobStatus.SUCCEEDED,
        "status": JobStatus.SUCCEEDED,
        "transcription_duration_seconds": transcription_duration_seconds,
        "model_identifier": model_identifier,
        "model_display_name": model_display_name,
        "updated_datetime": now,
    }
    if provenance is not None:
        values["provenance"] = provenance.model_copy(update={"generated_at": now}).model_dump(mode="json")
    changed = session.execute(
        sa_update(SpeechBatchJob)
        .where(SpeechBatchJob.id == job_id, col(SpeechBatchJob.status).in_(_IN_FLIGHT_JOB))
        .values(**values)
    )
    session.commit()
    return changed.rowcount == 1


def fail_multipart_job(session: Session, job_id: UUID, error_message: str) -> bool:
    """A part failed: the job FAILED, with ``error_message``. Conditional like complete_multipart_job
    (a job already finished or failed is left as it is), True only for the call that changed it."""
    changed = session.execute(
        sa_update(SpeechBatchJob)
        .where(SpeechBatchJob.id == job_id, col(SpeechBatchJob.status).in_(_IN_FLIGHT_JOB))
        .values(
            status=JobStatus.FAILED,
            batch_job_status=BatchJobStatus.FAILED,
            error_message=error_message,
            updated_datetime=datetime.now(UTC),
        )
    )
    session.commit()
    return changed.rowcount == 1


def fetch_jobs_with_part_leftovers(session: Session, limit: int = 5) -> list[UUID]:
    """Finished or failed jobs in parts whose part blobs or Azure jobs are still recorded: their
    cleanup did not finish (a crash, or a delete that failed): tried again."""
    leftover = exists().where(
        TranscriptionJobPart.job_id == SpeechBatchJob.id,
        or_(col(TranscriptionJobPart.blob_path).is_not(None), col(TranscriptionJobPart.batch_job_url).is_not(None)),
    )
    stmt = (
        select(SpeechBatchJob.id)
        .where(col(SpeechBatchJob.status).in_([JobStatus.SUCCEEDED, JobStatus.FAILED]), leftover)
        .order_by(col(SpeechBatchJob.updated_datetime).asc().nulls_first())
        .limit(limit)
    )
    return list(session.exec(stmt).all())
