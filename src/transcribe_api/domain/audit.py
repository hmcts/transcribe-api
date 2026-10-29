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
fail-closed. In async route handlers ``async with audited(...)`` runs the insert in a
worker thread and awaits it before the response is sent, so a slow database never
stalls the event loop and no row is lost to fire-and-forget.

A streamed export (``TRANSCRIPT_EXPORTED`` for ``/download`` and audio playback) is
recorded when the response body ends, not when the response object is returned:
``success`` once the last chunk is sent, ``failed`` with ``detail.reason``
``stream_error``, ``client_disconnected`` or ``cancelled`` otherwise
(``AuditedAction.streaming_response``). The insert runs in a worker thread.

An ``ACCESS_DENIED`` row from an ownership check outside an audited action is never
inserted on the event loop either (``record_audit``). Inside ``async with
deferred_audit()`` (e.g. the upload route's key checks) it is inserted in a worker
thread when the block exits, awaited, as for ``audited``. Elsewhere on the event loop
(synchronous helpers called from many async call sites) it is handed to a worker
thread as a task the module keeps a reference to, and the application's shutdown
awaits those tasks (``drain_pending_writes``); only a process killed before that
could lose such a row. Code not on the event loop (the poller's database calls,
``asyncio.to_thread`` workers) writes synchronously.

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

import asyncio
import contextlib
import logging
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

import anyio
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from hmcts_azure_auth.audit import AuditEvent, AuditEventType
from hmcts_otel import get_request_id, record_event
from hmcts_otel import conventions as c
from sqlmodel import Session

import transcribe_api.runtime.telemetry  # noqa: F401  (registers the merged schema)
from transcribe_api.runtime.db_connection import get_engine
from transcribe_api.domain.models_dictation import AuditLog

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator, Mapping
    from types import TracebackType
    from uuid import UUID

    from starlette.types import Receive, Scope, Send

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

# Why a streamed export failed after its response started (detail.reason)
STREAM_ERROR = "stream_error"  # the upstream (storage) stream raised
CLIENT_DISCONNECTED = "client_disconnected"  # the client went away before the end
STREAM_CANCELLED = "cancelled"  # the server cancelled the response (e.g. shutdown)

EVENT_WRITE_FAILED = "audit.write_failed"
ATTR_ACTION = "audit.action"


def _write_failed(action: str, exc: BaseException) -> None:
    # Types only: the row and the error text can carry identifiers.
    logger.error("AUDIT_WRITE_FAILED event_type=%s error=%s", action, type(exc).__name__)
    record_event(EVENT_WRITE_FAILED, {ATTR_ACTION: action, c.ERROR_CODE: EVENT_WRITE_FAILED})


def _persist(row: AuditLog) -> bool:
    """Insert one row in its own short transaction. Never raises (fail-open)."""
    try:
        with Session(get_engine()) as session:
            session.add(row)
            session.commit()
    except Exception as exc:
        _write_failed(row.event_type, exc)
        return False
    return True


def write_audit_event(event: AuditEvent) -> None:
    """Write an AuditEvent from the auth library or an admin route.

    Called synchronously by get_allowlisted_user on every 403. Failures are logged but
    do not propagate: a logging failure must never block a response.
    """
    try:  # building the row is part of the write: fail-open too
        row = AuditLog(
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
    except Exception as exc:
        _write_failed(str(event.event_type), exc)
        return
    _persist(row)


def record_audit(
    action: str,
    *,
    actor_id: UUID | str,
    outcome: str,
    object_type: str | None = None,
    object_id: UUID | str | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Write one audit row (DIAAT-286 fields). Never raises.

    Off the event loop the insert is synchronous: False if it failed. On the event
    loop it is never run there (a synchronous insert would stall every request): it is
    deferred to the enclosing ``deferred_audit()`` block, or else handed to a worker
    thread; True then means only that it was handed over (fail-open either way). The
    row, with its request ID, is built here, in the caller's context.
    """
    try:  # building the row is part of the write: fail-open too
        row = _audit_row(
            action, actor_id=actor_id, outcome=outcome, object_type=object_type, object_id=object_id, detail=detail
        )
    except Exception as exc:
        _write_failed(action, exc)
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _persist(row)  # not on an event loop: a worker thread, the poller's calls
    deferred = _deferred.get()
    if deferred is not None:
        deferred.append(row)
        return True
    task = loop.create_task(asyncio.to_thread(_persist, row))
    _pending_writes.add(task)  # referenced until done, and awaited on shutdown
    task.add_done_callback(_pending_writes.discard)
    return True


# Rows written from the event loop outside a deferred_audit() block, being inserted.
_pending_writes: set[asyncio.Task[bool]] = set()

_deferred: ContextVar[list[AuditLog] | None] = ContextVar("deferred_audit_rows", default=None)


async def drain_pending_writes() -> None:
    """Await the audit inserts handed to worker threads (application shutdown)."""
    while _pending_writes:
        await asyncio.gather(*list(_pending_writes), return_exceptions=True)


class deferred_audit:  # noqa: N801 - used as a function: ``async with deferred_audit():``
    """Collect the audit rows written in the block (e.g. by an ownership check outside
    an audited action) and insert them in a worker thread when it exits, awaited, so
    before the response is sent. For async routes that are not an audited action."""

    def __init__(self) -> None:
        self._rows: list[AuditLog] = []
        self._token: Token[list[AuditLog] | None] | None = None

    async def __aenter__(self) -> None:
        self._token = _deferred.set(self._rows)

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._token is not None:
            _deferred.reset(self._token)
            self._token = None
        for row in self._rows:
            await asyncio.to_thread(_persist, row)


def _audit_row(
    action: str,
    *,
    actor_id: UUID | str,
    outcome: str,
    object_type: str | None,
    object_id: UUID | str | None,
    detail: dict[str, Any] | None,
    request_id: str | None = None,
) -> AuditLog:
    """The row, with the request ID (and timestamp) of the calling request's context,
    or the request ID given (captured earlier, e.g. for a streamed response)."""
    return AuditLog(
        event_type=action,
        user_id=str(actor_id),
        outcome=outcome,
        object_type=object_type,
        object_id=str(object_id) if object_id is not None else None,
        detail=detail or None,
        request_id=request_id or get_request_id(),
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
        # Built in the request's context: a streamed body may be sent outside it.
        self._request_id = get_request_id()
        self._streams = False  # the outcome is settled when the response body ends
        self._stream_pending = False  # the row is still to be written by the stream

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

    def streaming_response(
        self,
        content: AsyncIterable[bytes],
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> StreamingResponse:
        """A response whose success is recorded when its body has been sent.

        Returned from the audited block: if the block ends without a refusal or
        failure, its row is not written then but when the response ends: ``success``
        after the last chunk is sent, else ``failed`` with ``detail.reason``
        ``stream_error`` (the upstream stream raised), ``client_disconnected`` or
        ``cancelled``. A block that ends refused or failed (e.g. ``fail()`` for a
        storage error status) is written when it exits, as before. One row either way.
        """
        self._streams = True
        return _AuditedStreamingResponse(self, content, status_code=status_code, headers=headers)

    def _conclude(self, exc: BaseException | None) -> AuditLog | None:
        """Settle the outcome; the row to write, or None (a skipped success, or one
        left to the streamed response)."""
        self.finished = True
        if exc is None:
            self.outcome = self.outcome or SUCCESS
        elif self.outcome != DENIED:
            refused = isinstance(exc, HTTPException) and exc.status_code in _NOT_FOUND_OR_REFUSED
            self.outcome = DENIED if refused and not self._authorised else FAILED
        if self.outcome == SUCCESS and self._streams:
            self._stream_pending = True  # not exported yet: the response writes the row
            return None
        if self.outcome == SUCCESS and self._skip_success:
            return None
        return self._row()

    def _conclude_stream(self, reason: str | None) -> AuditLog | None:
        """The streamed response ended (``reason`` None: completely sent)."""
        if not self._stream_pending:
            return None  # recorded when the block exited (or never streamed)
        self._stream_pending = False
        if reason is None:
            return None if self._skip_success else self._row()
        self.outcome = FAILED
        self.detail["reason"] = reason
        return self._row()

    def _row(self) -> AuditLog:
        return _audit_row(
            self.action,
            actor_id=self.actor_id,
            outcome=self.outcome or SUCCESS,
            object_type=self.object_type,
            object_id=self.object_id,
            detail=self.detail,
            request_id=self._request_id,
        )


class _AuditedStreamingResponse(StreamingResponse):
    """A StreamingResponse that concludes its audited action when the body ends.

    Starlette ends a streamed response in one of three ways: the body is sent (the
    call returns, the iterator exhausted); the iterator raises (the call raises); or
    the client disconnects. On a disconnect, ASGI spec 2.3 servers (uvicorn) cancel
    the sending task and the call returns with the iterator not exhausted; spec 2.4
    servers make ``send`` raise OSError (``ClientDisconnect``).
    """

    def __init__(
        self,
        audit: AuditedAction,
        content: AsyncIterable[bytes],
        *,
        status_code: int,
        headers: Mapping[str, str] | None,
    ) -> None:
        self._audit = audit
        self._exhausted = False
        self._iterator_failed = False
        self._source = content
        super().__init__(self._watched(content), status_code=status_code, headers=headers)

    async def _watched(self, content: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
        try:
            async for chunk in content:
                yield chunk
        except Exception:
            self._iterator_failed = True
            raise
        self._exhausted = True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        reason: str | None = None
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            reason = STREAM_CANCELLED
            raise
        except Exception:
            reason = STREAM_ERROR if self._iterator_failed else CLIENT_DISCONNECTED
            raise
        else:
            if not self._exhausted:
                reason = CLIENT_DISCONNECTED  # cancelled by the server on a disconnect
        finally:
            # Shielded: the response's own cancellation must not lose the row or leave
            # the upstream connection open.
            with anyio.CancelScope(shield=True):
                await _aclose(self.body_iterator)
                await _aclose(self._source)
                row = self._audit._conclude_stream(reason)  # noqa: SLF001
                if row is not None:
                    await anyio.to_thread.run_sync(_persist, row)


async def _aclose(iterator: object) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        with contextlib.suppress(Exception):  # closing only; the outcome is recorded
            await aclose()


_current: ContextVar[AuditedAction | None] = ContextVar("audited_action", default=None)


class audited:  # noqa: N801 - used as a function: ``async with audited(...)``
    """Audit the enclosed action: one row when it ends, whatever happens.

    ``async with`` (route handlers): the row is built on the event loop, so it carries
    the request's ID, and inserted in a worker thread, awaited before the block exits
    (and so before the response is sent). Plain ``with`` writes it synchronously, for
    code that is not on the event loop.
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
        self._audit = AuditedAction(
            action, actor_id=actor_id, object_type=object_type, object_id=object_id, detail=detail
        )
        self._token: Token[AuditedAction | None] | None = None

    def _enter(self) -> AuditedAction:
        self._token = _current.set(self._audit)
        return self._audit

    def _exit(self, exc: BaseException | None) -> AuditLog | None:
        try:
            return self._audit._conclude(exc)  # noqa: SLF001
        finally:
            if self._token is not None:
                _current.reset(self._token)
                self._token = None

    def __enter__(self) -> AuditedAction:
        return self._enter()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        row = self._exit(exc)
        if row is not None:
            _persist(row)

    async def __aenter__(self) -> AuditedAction:
        return self._enter()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        row = self._exit(exc)
        if row is not None:
            # Off the event loop: a slow database must not stall other requests. Awaited,
            # not fire-and-forget: a row lost on shutdown is a gap in the audit log.
            await asyncio.to_thread(_persist, row)


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
