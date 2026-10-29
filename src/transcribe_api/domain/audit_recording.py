"""Audit records of sensitive actions on jobs (DIAAT-286).

Who (``actor_id``, the internal ``User.id``; never an email or a name) did what
(``action``) to which job (``object_type`` / ``object_id``), when (``timestamp``),
whether it worked (``outcome``: success, denied or failed) and in which request
(``request_id``, the ``hmcts.request.id`` that also tags the request's telemetry and
any ``security.ownership_violation`` event). ``detail`` holds only controlled values
(a reason code, an HTTP status); never transcript text, file names or case references.

Audited actions (the vocabulary is ``runtime/telemetry_schema.AUDIT_ACTIONS``):

* ``transcript.view``: a transcript was returned by the job detail (GET /jobs/{id});
* ``audio.export``: the job's audio was streamed back (the only audio egress);
* ``job.delete``: a job and its audio were deleted;
* ``job.submit``: a transcription job was submitted;
* ``transcript.edit``: recorded only when refused (corrections and baselines).

A refusal (outcome ``denied``) is recorded wherever DIAAT-285 records an ownership
violation (``blocked`` and ``detected``), including the job list's ``detected``
assertion. A failure after authorisation is ``failed``.

The job list (GET /jobs) is NOT recorded as a view. The dashboard polls it every 5 s
while any job is in flight, so one row per returned transcript per poll would bury
the real sensitive events and grow the table without bound. OPEN SECURITY QUESTION:
should a list view count as a view? It matters because GET /jobs returns the full
transcripts (dialogue entries) of every job on the page, which the dashboard does not
need: removing them from the list response (a data-exposure fix) would make the
question moot.

Decision OD7: audit records are stored with the record, in the application database,
not in monitoring telemetry: the shared ``audit_log`` table (one table for both areas,
domain/audit.py; the recording-only table is not created). The recording fields map
onto it: ``action`` -> ``event_type``, ``actor_id`` -> ``user_id`` (as text), and the
rest by name. Rows are written by domain/audit.py's core: off the event loop, and
fail-open.

Writing is fail-open: if the write fails the request carries on, the failure is logged
(exception type only) and ``audit.write_failed`` is emitted (the action only). Whether
a sensitive action should instead be refused when it cannot be audited (fail-closed)
is a security decision that has not been taken.

Append-only: the writers only insert. Nothing in the application updates or deletes
audit rows.

Access and retention (AC3): PENDING the security and data decision. There is no
retention or purge mechanism, and no API to read audit records: access is direct
database access (the same access as the transcripts themselves). Do not add a purge
or a read API without that decision.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from hmcts_otel import conventions as c
from hmcts_otel import get_request_id, record_event

from transcribe_api.domain import audit as audit_core
from transcribe_api.domain.models_dictation import AuditLog

logger = logging.getLogger(__name__)

AuditAction = Literal["job.submit", "transcript.view", "transcript.edit", "audio.export", "job.delete"]
AuditOutcome = Literal["success", "denied", "failed"]
AuditObjectType = Literal["job", "upload"]
SecurityResourceType = Literal["job", "transcript", "audio", "upload"]
SecurityAction = Literal["read", "write", "delete", "submit"]

# Why a request was refused. not_owner: the job (or upload) belongs to another user;
# legacy_job: a job from before per-user ownership, which only administrators may
# reach; ownership_assertion_failed: authorisation let it through but the
# post-authorisation assertion caught it (DIAAT-285 "detected", an authorisation defect).
DenialReason = Literal["not_owner", "legacy_job", "ownership_assertion_failed"]

# The DIAAT-285 security event's (resource type, action) as the audited action.
_ACTION_FOR_VIOLATION: dict[tuple[str, str], AuditAction] = {
    ("transcript", "read"): "transcript.view",
    ("transcript", "write"): "transcript.edit",
    ("audio", "read"): "audio.export",
    ("job", "delete"): "job.delete",
    ("job", "read"): "transcript.view",
    ("upload", "submit"): "job.submit",
}


# For a pair not in the table: by the security action alone, so a new call site never
# turns a refusal (404) into an error (500) while its row is built.
_ACTION_BY_SECURITY_ACTION: dict[str, AuditAction] = {
    "read": "transcript.view",
    "write": "transcript.edit",
    "delete": "job.delete",
    "submit": "job.submit",
}


def action_for_violation(resource_type: SecurityResourceType, action: SecurityAction) -> AuditAction:
    """The audited action that a DIAAT-285 ownership violation was an attempt at."""
    audited_action = _ACTION_FOR_VIOLATION.get((resource_type, action))
    if audited_action is None:
        logger.warning("No audited action for violation %s/%s; mapped by action", resource_type, action)
        audited_action = _ACTION_BY_SECURITY_ACTION.get(action, "transcript.view")
    return audited_action


@dataclass(frozen=True)
class AuditRecord:
    """One audit row, before it is stamped with the time and the request ID."""

    action: AuditAction
    outcome: AuditOutcome
    actor_id: UUID
    object_id: UUID | None
    object_type: AuditObjectType = "job"
    detail: dict[str, object] | None = None


def denied(
    action: AuditAction,
    actor_id: UUID,
    object_id: UUID | None,
    reason: DenialReason,
    object_type: AuditObjectType = "job",
) -> AuditRecord:
    return AuditRecord(action, "denied", actor_id, object_id, object_type, {"reason": reason})


def _request_id() -> str | None:
    """As the shared writer stores it (domain/audit.py), so one request's rows from
    either area join to its telemetry the same way."""
    return get_request_id()


def write_audit(*records: AuditRecord) -> None:
    """Write the records through the shared writer (domain/audit.py). Never raises.

    Each row in its own short transaction, off the event loop, fail-open; all rows
    share one timestamp and the request ID. Its own session(s), so a refusal or a
    failure is recorded even when the request's transaction is rolled back.
    """
    if not records:
        return
    try:
        timestamp = datetime.now(UTC)
        request_id = _request_id()
        rows = [
            {
                "id": uuid4(),
                "timestamp": timestamp,
                "actor_id": record.actor_id,
                "action": record.action,
                "outcome": record.outcome,
                "object_type": record.object_type,
                "object_id": record.object_id,
                "request_id": request_id,
                "detail": record.detail,
            }
            for record in records
        ]
        _insert(rows)
    except Exception as exc:  # an audit failure must never break the user's request
        _write_failed(records[0].action, exc)


def _insert(rows: list[dict[str, object]]) -> None:
    """Hand the rows to the shared writer (domain/audit.py), as audit_log rows."""
    audit_core.persist_rows(
        [
            AuditLog(
                id=row["id"],
                timestamp=row["timestamp"],
                event_type=row["action"],
                user_id=str(row["actor_id"]),
                outcome=row["outcome"],
                object_type=row["object_type"],
                object_id=str(row["object_id"]) if row["object_id"] is not None else None,
                request_id=row["request_id"],
                detail=row["detail"],
            )
            for row in rows
        ]
    )


def _write_failed(action: AuditAction, exc: BaseException) -> None:
    try:
        # The exception type only: a database error message can quote the row.
        logger.error("Audit record could not be written (%s)", type(exc).__name__)
        record_event(
            audit_core.EVENT_WRITE_FAILED,
            {audit_core.ATTR_ACTION: action, c.ERROR_CODE: audit_core.EVENT_WRITE_FAILED},
        )
    except Exception:  # noqa: BLE001, S110 - reporting the failure must not raise either
        pass


@dataclass
class AuditScope:
    """The audited block's result: set ``object_id``/``detail``, or ``skip()`` it."""

    object_id: UUID | None = None
    detail: dict[str, object] = field(default_factory=dict)
    skipped: bool = False

    def skip(self) -> None:
        """No record from this scope: nothing sensitive happened (e.g. the job has no
        transcript yet), or the block wrote its own record (a refusal)."""
        self.skipped = True


@contextmanager
def audited(
    action: AuditAction,
    actor_id: UUID,
    object_id: UUID | None = None,
    object_type: AuditObjectType = "job",
) -> Iterator[AuditScope]:
    """Record the block, which runs after authorisation, as one audit row.

    success if it completes; failed if it raises (detail.reason: http_<status> for an
    HTTPException, else "error"), and the exception propagates unchanged. Nothing if
    the block called ``skip()``.
    """
    scope = AuditScope(object_id=object_id)
    try:
        yield scope
    except HTTPException as exc:
        _failed(scope, action, actor_id, object_type, f"http_{exc.status_code}")
        raise
    except asyncio.CancelledError:
        # The request was cancelled mid-action (client gone, shutdown): what was done
        # may be partial, so it is recorded, as failed.
        _failed(scope, action, actor_id, object_type, "cancelled")
        raise
    except Exception:
        _failed(scope, action, actor_id, object_type, "error")
        raise
    if not scope.skipped:
        write_audit(AuditRecord(action, "success", actor_id, scope.object_id, object_type, scope.detail or None))


def _failed(
    scope: AuditScope,
    action: AuditAction,
    actor_id: UUID,
    object_type: AuditObjectType,
    reason: str,
) -> None:
    if scope.skipped:  # the block recorded its own outcome (e.g. a refusal)
        return
    detail = {**scope.detail, "reason": reason}
    write_audit(AuditRecord(action, "failed", actor_id, scope.object_id, object_type, detail))
