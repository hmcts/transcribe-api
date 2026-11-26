"""Hearings with several recording days (DIAAT-428).

A hearing (the ``hearing`` table) is what a Judge sees as one recording: its days are the jobs
(``transcription_job``) whose ``hearing_id`` names it, each a recording of its own with its own
transcript, ordered by ``hearing_date``, then upload time, then id. Every new job is a day of a
hearing: of a new one whose id is the job's, or of one of its owner's that it is added to.

Rolling deploys: ``hearing_id`` and ``hearing_date`` are nullable until a later migration makes them
required, so a job written by the previous release has neither. Such a job is a one-day hearing whose id
is the job's, dated the day it was uploaded in London (``hearing_of``, ``day_date``, ``find_hearing``):
the hearing 0019 would have made of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, distinct, func, or_
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer
from sqlmodel import Session, col, select

from transcribe_api.domain.models_recording import Hearing, JobStatus, SpeechBatchJob

LONDON = ZoneInfo("Europe/London")

DayState = Literal["processing", "complete", "failed"]
HearingStatus = Literal["processing", "completed", "failed", "partial"]

# What a hearing keeps of its first recording's metadata; the rest (the file name) is each day's own.
HEARING_METADATA_KEYS = ("case_reference", "tribunal")


def _now() -> datetime:
    return datetime.now(UTC)


def london_date(moment: datetime) -> date:
    if moment.tzinfo is None:  # SQLite returns naive values; they are UTC
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(LONDON).date()


def today_in_london() -> date:
    return london_date(_now())


def hearing_of(job: SpeechBatchJob) -> UUID:
    return job.hearing_id or job.id


def day_date(job: SpeechBatchJob) -> date:
    return job.hearing_date or london_date(job.created_datetime)


def _created_utc(job: SpeechBatchJob) -> datetime:
    created = job.created_datetime
    return created.replace(tzinfo=UTC) if created.tzinfo is None else created


def order_days(days: list[SpeechBatchJob]) -> list[SpeechBatchJob]:
    return sorted(days, key=lambda job: (day_date(job), _created_utc(job), str(job.id)))


def day_state(job: SpeechBatchJob) -> DayState:
    if job.status == JobStatus.SUCCEEDED:
        return "complete"
    if job.status == JobStatus.FAILED:
        return "failed"
    return "processing"


def hearing_status(days: list[SpeechBatchJob]) -> HearingStatus:
    states = [day_state(day) for day in days]
    if "processing" in states:
        return "processing"
    if all(state == "complete" for state in states):
        return "completed"
    if all(state == "failed" for state in states):
        return "failed"
    return "partial"


def hearing_metadata(metadata: dict | None) -> dict:
    return {key: metadata[key] for key in HEARING_METADATA_KEYS if (metadata or {}).get(key) is not None}


@dataclass(frozen=True)
class HearingRecord:
    """A hearing as the routes see it. ``stored`` is False for a job from before hearings."""

    id: UUID
    user_id: UUID | None
    jurisdiction_id: UUID | None
    metadata: dict = field(default_factory=dict)
    created_datetime: datetime | None = None
    updated_datetime: datetime | None = None
    stored: bool = True


def _record(hearing: Hearing) -> HearingRecord:
    return HearingRecord(
        hearing.id,
        hearing.user_id,
        hearing.jurisdiction_id,
        hearing.metadata_ or {},
        hearing.created_datetime,
        hearing.updated_datetime,
    )


def _unstored(job: SpeechBatchJob) -> HearingRecord:
    return HearingRecord(
        job.id,
        job.user_id,
        job.jurisdiction_id,
        hearing_metadata(job.metadata_),
        job.created_datetime,
        job.updated_datetime,
        stored=False,
    )


def _without_transcripts():
    return (defer(SpeechBatchJob.dialogue_entries), defer(SpeechBatchJob.baseline_transcript))  # type: ignore[arg-type]


def find_hearing(session: Session, hearing_id: UUID, *, lock: bool = False) -> HearingRecord | None:
    """The hearing, or the job from before hearings with this id. ``lock`` holds the hearing's row
    against deletion until the transaction ends (a day being added to it)."""
    query = select(Hearing).where(Hearing.id == hearing_id)
    if lock:
        query = query.with_for_update(read=True, key_share=True)
    hearing = session.exec(query).first()
    if hearing is not None:
        return _record(hearing)
    job = session.exec(
        select(SpeechBatchJob)
        .options(*_without_transcripts())
        .where(SpeechBatchJob.id == hearing_id, col(SpeechBatchJob.hearing_id).is_(None))
    ).first()
    return _unstored(job) if job is not None else None


def new_hearing(session: Session, *, hearing_id: UUID, user_id: UUID, jurisdiction_id: UUID, metadata: dict) -> Hearing:
    """Add a hearing to the session, to be committed with its first day."""
    hearing = Hearing(
        id=hearing_id, user_id=user_id, jurisdiction_id=jurisdiction_id, metadata_=hearing_metadata(metadata)
    )
    session.add(hearing)
    return hearing


def store_unstored_hearing(session: Session, record: HearingRecord) -> None:
    """Make the job from before hearings a stored one-day hearing (a day is being added to it), as 0019
    would have. Committed with the new day."""
    session.add(
        Hearing(
            id=record.id,
            user_id=record.user_id,
            jurisdiction_id=record.jurisdiction_id,
            metadata_=record.metadata,
            created_datetime=record.created_datetime or _now(),
        )
    )
    job = session.get(SpeechBatchJob, record.id)
    if job is not None and job.hearing_id is None:
        job.hearing_id = record.id
        job.hearing_date = day_date(job)
        session.add(job)


def touch_hearing(session: Session, hearing_id: UUID) -> None:
    """A day was added, deleted or redated: the hearing changed. Committed with the change."""
    session.execute(sa_update(Hearing).where(col(Hearing.id) == hearing_id).values(updated_datetime=_now()))


def _in_hearing(hearing_id: UUID):
    return or_(
        col(SpeechBatchJob.hearing_id) == hearing_id,
        and_(col(SpeechBatchJob.id) == hearing_id, col(SpeechBatchJob.hearing_id).is_(None)),
    )


def days_of(session: Session, hearing_id: UUID, *, transcripts: bool = True) -> list[SpeechBatchJob]:
    query = select(SpeechBatchJob).where(_in_hearing(hearing_id))
    if not transcripts:
        query = query.options(*_without_transcripts())
    return order_days(list(session.exec(query).all()))


def day_count(session: Session, hearing_id: UUID) -> int:
    return session.exec(select(func.count(SpeechBatchJob.id)).where(_in_hearing(hearing_id))).one()  # type: ignore[arg-type]


@dataclass(frozen=True)
class HearingEntry:
    hearing: HearingRecord
    days: list[SpeechBatchJob]


def list_hearings_paginated(
    session: Session, user_id: UUID, jurisdiction_id: UUID, *, limit: int, offset: int
) -> tuple[list[HearingEntry], int]:
    """One user's hearings in one jurisdiction, the one with the latest upload first, each with its days
    in order and without their transcripts."""
    key = func.coalesce(SpeechBatchJob.hearing_id, SpeechBatchJob.id)
    mine = (col(SpeechBatchJob.user_id) == user_id, col(SpeechBatchJob.jurisdiction_id) == jurisdiction_id)
    total = session.exec(select(func.count(distinct(key))).where(*mine)).one()
    latest = func.max(SpeechBatchJob.created_datetime)
    keys = [
        row[0]
        for row in session.exec(
            select(key, latest)
            .where(*mine)
            .group_by(key)
            .order_by(latest.desc(), key.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    ]
    if not keys:
        return [], total
    days: dict[UUID, list[SpeechBatchJob]] = {k: [] for k in keys}
    for job in session.exec(select(SpeechBatchJob).options(*_without_transcripts()).where(*mine, key.in_(keys))).all():
        days[hearing_of(job)].append(job)
    stored = {h.id: _record(h) for h in session.exec(select(Hearing).where(col(Hearing.id).in_(keys))).all()}
    entries = []
    for k in keys:
        ordered = order_days(days[k])
        entries.append(HearingEntry(stored.get(k) or _unstored(ordered[0]), ordered))
    return entries, total


def delete_hearing_if_empty(session: Session, hearing_id: UUID) -> bool:
    """Delete the hearing row once no day is left. False if a day is left (or was added meanwhile: the
    foreign key refuses the delete)."""
    if day_count(session, hearing_id):
        return False
    try:
        session.execute(sa_delete(Hearing).where(col(Hearing.id) == hearing_id))
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True
