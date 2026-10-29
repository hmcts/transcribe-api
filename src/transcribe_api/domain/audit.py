"""Audit records: who did what to which record, when, and whether it worked.

Stored in the application database (``audit_log``), not in monitoring telemetry,
which is sampled, capped and expires (DIAAT-286, decision OD7).

What is recorded
----------------
* ``ACCESS_DENIED`` from the auth library (a role check refused a request; written
  by ``write_audit_event``, the ``audit_writer`` of ``get_allowlisted_user``), and
  from the ownership checks of DIAAT-285 (``security.ownership_violation``): the
  same call sites write the audit row, with the identifiers the telemetry event
  deliberately leaves out.
* ``ROLE_CHANGE`` (written by the auth library) and ``user.delete`` (admin routes).
* ``transcript.view``, ``transcript.export``, ``transcript.delete``: the sensitive
  actions on a transcription, its transcript or its decision document (``audited``).

App actions are ``<object>.<verb>`` (runtime/telemetry_schema.AUDIT_ACTIONS, one
vocabulary with the recording area); the auth library's ``ACCESS_DENIED`` and
``ROLE_CHANGE`` cannot change.

Each DIAAT-286 row has:

* ``user_id``: the actor, as the internal user UUID (``user.id``). Not the email: the
  users table maps one to the other, and the audit log stays free of contact
  details. This is the proposed "approved actor identifier", pending the linked
  security and data decision. (Rows written by the auth library carry the Azure
  object ID, as before.)
* ``event_type`` (the action), ``outcome`` (``success``, ``denied`` or ``failed``),
  ``timestamp`` (UTC);
* ``object_type`` and ``object_id``: the transcription UUID (or another record UUID),
  never a title, file name or blob path;
* ``request_id``: ``hmcts.request.id``, the join to telemetry, e.g. from a
  ``security.ownership_violation`` event (which carries no identifiers) to the row
  that says who and what;
* ``detail``: controlled values only (how, why refused); never content.

Writing never breaks the user's request (fail-open): a failed write logs the event
and error types only, records ``audit.write_failed`` (no identifiers) and the
request carries on. Whether a failed audit write should block the action instead is
a security decision still to be confirmed.

One insert per audited request, in its own short transaction after the action: in
the action's transaction, a failed audit write would roll the action back, which is
fail-closed.

Access and retention (AC3)
--------------------------
Pending the linked security and data decision (open question).

* Access: database access only. No route reads ``audit_log``; the admin routes write
  it but do not show it.
* Retention: none is implemented. Nothing deletes ``audit_log`` rows (the table has
  no purge job and no foreign keys, so rows outlive the users and transcriptions they
  name). Database backups follow the PostgreSQL server's backup retention
  (transcribe-shared-infrastructure).

Investigation queries (PostgreSQL)::

    -- Did user X view, export or delete transcription T, and did it succeed?
    SELECT timestamp, event_type, outcome, object_type, request_id, detail
    FROM audit_log
    WHERE object_id = '<transcription uuid>' ORDER BY timestamp;

    -- Everything one user did (internal user UUID; users.email gives the person).
    SELECT * FROM audit_log WHERE user_id = '<user uuid>' ORDER BY timestamp;

    -- From a security.ownership_violation alert to who and what.
    SELECT * FROM audit_log WHERE request_id = '<hmcts.request.id>';
"""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from hmcts_azure_auth.audit import AuditEvent, AuditEventType
from hmcts_otel import get_request_id, record_event
from hmcts_otel import conventions as c
from sqlmodel import Session

import transcribe_api.runtime.telemetry  # noqa: F401  (registers the merged schema)
from transcribe_api.runtime.db_connection import get_engine
from transcribe_api.domain.models_dictation import AuditLog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

logger = logging.getLogger(__name__)

# Actions (event_type). The names are historical; the values are the merged
# vocabulary (heritage TRANSCRIPT_VIEWED, TRANSCRIPT_EXPORTED, TRANSCRIPT_DELETED and
# USER_DELETED became these; the DIAAT-289 ETL maps old USER_DELETED rows).
ACCESS_DENIED = AuditEventType.ACCESS_DENIED.value
TRANSCRIPT_VIEWED = "transcript.view"
TRANSCRIPT_EXPORTED = "transcript.export"
TRANSCRIPT_DELETED = "transcript.delete"
USER_DELETED = "user.delete"

# Outcomes
SUCCESS = "success"
DENIED = "denied"
FAILED = "failed"

# Object types (beyond the security resource types: transcription, document, audio...)
TRANSCRIPT = "transcript"  # the transcript text (dialogue entries)
SECTIONED_TRANSCRIPT = "sectioned_transcript"

_NOT_FOUND_OR_REFUSED = frozenset({403, 404})

EVENT_WRITE_FAILED = "audit.write_failed"
ATTR_ACTION = "audit.action"


def _persist(row: AuditLog) -> bool:
    """Insert one row in its own short transaction. Never raises (fail-open)."""
    try:
        with Session(get_engine()) as session:
            session.add(row)
            session.commit()
    except Exception as exc:
        # Types only: the row and the error text can carry identifiers.
        logger.error("AUDIT_WRITE_FAILED event_type=%s error=%s", row.event_type, type(exc).__name__)  # noqa: TRY400
        record_event(EVENT_WRITE_FAILED, {ATTR_ACTION: row.event_type, c.ERROR_CODE: EVENT_WRITE_FAILED})
        return False
    return True


def write_audit_event(event: AuditEvent) -> None:
    """Write an AuditEvent from the auth library or an admin route.

    Called synchronously by get_allowlisted_user on every 403. Failures are logged but
    do not propagate: a logging failure must never block a response.
    """
    _persist(
        AuditLog(
            timestamp=event.timestamp,
            event_type=event.event_type,
            user_id=event.user_id,
            email=event.email,
            held_roles=event.held_roles,
            required_roles=event.required_roles,
            resource=event.resource,
            detail=event.detail,
            client_ip=event.client_ip,
            outcome=DENIED if event.event_type == ACCESS_DENIED else SUCCESS,
            request_id=get_request_id(),
        )
    )


def record_audit(
    action: str,
    *,
    actor_id: UUID | str,
    outcome: str,
    object_type: str | None = None,
    object_id: UUID | str | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Write one audit row (DIAAT-286 fields). Never raises; False if not written."""
    return _persist(
        AuditLog(
            event_type=action,
            user_id=str(actor_id),
            outcome=outcome,
            object_type=object_type,
            object_id=str(object_id) if object_id is not None else None,
            detail=detail or None,
            request_id=get_request_id(),
        )
    )


class AuditedAction:
    """One sensitive action of one request; written once, when the action ends.

    Outcome: ``success``, unless the action raised (``failed``), or was refused
    (``denied``): an ownership check fired (``ownership_violation``), ``deny()`` was
    called, or the record lookup refused it (403/404 before ``authorised()``).
    """

    def __init__(
        self,
        action: str,
        *,
        actor_id: UUID | str,
        object_type: str,
        object_id: UUID | str | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.action = action
        self.actor_id = actor_id
        self.object_type = object_type
        self.object_id = object_id
        self.detail: dict[str, Any] = dict(detail or {})
        self.outcome: str | None = None
        self._authorised = False
        self._skip_success = False
        self.finished = False

    def authorised(self) -> None:
        """The caller may act on the record: a later error is ``failed``, not ``denied``."""
        self._authorised = True

    def deny(self, reason: str | None = None) -> None:
        self.outcome = DENIED
        if reason:
            self.detail["reason"] = reason

    def fail(self, reason: str | None = None) -> None:
        if self.outcome != DENIED:
            self.outcome = FAILED
        if reason:
            self.detail["reason"] = reason

    def ownership_violation(self, outcome: str) -> None:
        """Called by the DIAAT-285 checks (``blocked`` or ``detected``)."""
        self.deny()
        self.detail["ownership_violation"] = outcome

    def skip_if_successful(self) -> None:
        """Do not write a success (e.g. a seek within a playback already recorded)."""
        self._skip_success = True

    def _finish(self, exc: BaseException | None) -> None:
        self.finished = True
        if exc is None:
            self.outcome = self.outcome or SUCCESS
        elif self.outcome != DENIED:
            refused = isinstance(exc, HTTPException) and exc.status_code in _NOT_FOUND_OR_REFUSED
            self.outcome = DENIED if refused and not self._authorised else FAILED
        if self.outcome == SUCCESS and self._skip_success:
            return
        record_audit(
            self.action,
            actor_id=self.actor_id,
            outcome=self.outcome,
            object_type=self.object_type,
            object_id=self.object_id,
            detail=self.detail,
        )


_current: ContextVar[AuditedAction | None] = ContextVar("audited_action", default=None)


@contextlib.contextmanager
def audited(
    action: str,
    *,
    actor_id: UUID | str,
    object_type: str,
    object_id: UUID | str | None,
    detail: dict[str, Any] | None = None,
) -> Iterator[AuditedAction]:
    """Audit the enclosed action: one row when it ends, whatever happens."""
    audit = AuditedAction(action, actor_id=actor_id, object_type=object_type, object_id=object_id, detail=detail)
    token = _current.set(audit)
    try:
        yield audit
    except BaseException as exc:
        audit._finish(exc)  # noqa: SLF001
        raise
    else:
        audit._finish(None)  # noqa: SLF001
    finally:
        _current.reset(token)


def note_ownership_violation(
    resource_type: str,
    action: str,
    outcome: str,
    *,
    actor_id: UUID | str | None,
    object_id: UUID | str | None,
) -> None:
    """Audit a DIAAT-285 ownership refusal (called by security_events).

    Inside an audited action, that action is marked denied (one row per request).
    Otherwise an ``ACCESS_DENIED`` row is written when the actor is known.
    """
    current = _current.get()
    if current is not None and not current.finished:  # (a task spawned by it may outlive it)
        current.ownership_violation(outcome)
        return
    if actor_id is None:
        return
    record_audit(
        ACCESS_DENIED,
        actor_id=actor_id,
        outcome=DENIED,
        object_type=resource_type,
        object_id=object_id,
        detail={"action": action, "ownership_violation": outcome},
    )
