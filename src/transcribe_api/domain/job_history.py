"""A recording's audit history, for its owner (DIAAT-395).

``GET /api/v1/jobs/{id}/history`` answers from ``audit_log``: the rows of the actions
``HISTORY_ACTIONS`` whose object is the recording, newest first. A row says what happened and
when, who did it, as which role and how it ended. **No transcript text is in a row or in the
answer**: the audit writers record controlled values only (a kind of edit, a segment ID, word
positions), and this reads nothing else.

Only the owner reads it, acting as a Judge in the recording's jurisdiction (``recording.audit.read``):
the route answers anyone else as it does for a recording they cannot see. An action by anyone but
the owner (a refused attempt to view, edit or play it) is shown without who it was: the owner learns
that someone tried, not who.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_
from sqlmodel import Session, select

from transcribe_api.domain.models_dictation import AuditLog

# What the history shows: the recording audit actions (domain/audit_recording.py).
HISTORY_ACTIONS = (
    "job.submit",
    "transcript.view",
    "transcript.edit",
    "transcript.rename_speaker",
    "transcript.rollback",
    "transcript.accept",
    "audio.export",
    "job.delete",
    "hearing.add_recording",
    "hearing.edit",
    "hearing.delete",
)

# The audit object types the recording writers use (domain/audit_recording.py ``AuditObjectType``). Dictation's
# heritage rows share some action names (``transcript.view``) but record a ``transcription``: not a recording.
HISTORY_OBJECT_TYPES = ("job", "upload", "hearing")

ANOTHER_USER = "Another user"


@dataclass(frozen=True)
class HistoryItem:
    id: UUID
    timestamp: datetime
    action: str
    actor_label: str
    actor_role: str | None
    outcome: str
    # The recording day the row is about (DIAAT-428): a job's own rows, or the day a hearing row names.
    job_id: UUID | None = None


def job_history(
    session: Session, job_id: UUID, owner_id: UUID, owner_label: str, *, page: int, page_size: int
) -> tuple[list[HistoryItem], int]:
    """The audit rows of the recording, newest first, and how many there are."""
    return _history(session, AuditLog.object_id == str(job_id), owner_id, owner_label, page=page, page_size=page_size)


def hearing_history(
    session: Session,
    hearing_id: UUID,
    day_ids: list[UUID],
    owner_id: UUID,
    owner_label: str,
    *,
    page: int,
    page_size: int,
) -> tuple[list[HistoryItem], int]:
    """The audit rows of a hearing (DIAAT-428), newest first: the hearing's own, its days', and those of the
    days deleted from it (found through their ``job.delete`` rows, which name the hearing)."""
    deleted_days = select(AuditLog.object_id).where(
        AuditLog.event_type == "job.delete",
        AuditLog.object_type == "job",
        AuditLog.detail["hearing_id"].as_string() == str(hearing_id),  # type: ignore[index]
    )
    objects = [str(hearing_id), *(str(day) for day in day_ids)]
    about = or_(AuditLog.object_id.in_(objects), AuditLog.object_id.in_(deleted_days))  # type: ignore[union-attr]
    return _history(session, about, owner_id, owner_label, page=page, page_size=page_size)


def _day_of(row: AuditLog) -> UUID | None:
    raw = row.object_id if row.object_type == "job" else (row.detail or {}).get("job_id")
    try:
        return UUID(str(raw)) if raw else None
    except ValueError:
        return None


def _history(
    session: Session, about, owner_id: UUID, owner_label: str, *, page: int, page_size: int
) -> tuple[list[HistoryItem], int]:
    wanted = (
        about
        & AuditLog.event_type.in_(HISTORY_ACTIONS)  # type: ignore[attr-defined]
        & AuditLog.object_type.in_(HISTORY_OBJECT_TYPES)  # type: ignore[union-attr]
    )
    total = session.exec(select(func.count(AuditLog.id)).where(wanted)).one()  # type: ignore[arg-type]
    rows = session.exec(
        select(AuditLog)
        .where(wanted)
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())  # type: ignore[attr-defined]
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = []
    for row in rows:
        mine = row.user_id == str(owner_id)
        items.append(
            HistoryItem(
                id=row.id,
                timestamp=row.timestamp,
                action=row.event_type,
                actor_label=owner_label if mine else ANOTHER_USER,
                actor_role=row.active_role if mine else None,
                outcome=row.outcome or "success",
                job_id=_day_of(row),
            )
        )
    return items, total
