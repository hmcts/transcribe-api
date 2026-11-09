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

from sqlalchemy import func
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
)

ANOTHER_USER = "Another user"


@dataclass(frozen=True)
class HistoryItem:
    id: UUID
    timestamp: datetime
    action: str
    actor_label: str
    actor_role: str | None
    outcome: str


def job_history(
    session: Session, job_id: UUID, owner_id: UUID, owner_label: str, *, page: int, page_size: int
) -> tuple[list[HistoryItem], int]:
    """The audit rows of the recording, newest first, and how many there are."""
    wanted = (AuditLog.object_id == str(job_id)) & AuditLog.event_type.in_(HISTORY_ACTIONS)  # type: ignore[attr-defined]
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
            )
        )
    return items, total
