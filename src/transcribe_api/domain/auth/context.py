"""The context a request is made in (DIAAT-390): errors, advisory headers, audit stamping.

A user acts in one (jurisdiction, role) context at a time, chosen after sign-in and carried
in the BenchNotes token (domain/auth/app_token.py). Around it:

* ``context_required`` (403): a route that needs a context was reached with none chosen.
* ``context_changed`` (409): the browser sends ``X-Jurisdiction: <code|->`` and ``X-Role:
  <role>`` for the context its tab is in. They are advisory: absent is fine, but if present
  and different from the token's context, the tab is stale (another tab switched) and must
  not act: it reloads.
* ``X-Requested-Context: <jurisdiction|->:<role>``: sent when a new token is minted, so the
  session keeps the context it chose; honoured only if the user still holds it.
* the context an audit row is stamped with (``record_audit``): set per request by the auth
  dependency, read where the row is built.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Request

from transcribe_api.domain.auth.app_token import Context

CONTEXT_REQUIRED = "context_required"
CONTEXT_CHANGED = "context_changed"
CONTEXT_NOT_AVAILABLE = "context_not_available"

HEADER_JURISDICTION = "X-Jurisdiction"
HEADER_ROLE = "X-Role"
HEADER_REQUESTED_CONTEXT = "X-Requested-Context"
# The header's spelling of "no jurisdiction" (a global role).
GLOBAL = "-"


def error(status_code: int, code: str, message: str) -> HTTPException:
    """The API's error shape: ``{"detail": {"code": ..., "message": ...}}``."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def context_required() -> HTTPException:
    return error(403, CONTEXT_REQUIRED, "Choose the jurisdiction and role you are working as first.")


def context_changed() -> HTTPException:
    return error(409, CONTEXT_CHANGED, "The jurisdiction or role you are working as has changed. Reload the page.")


def context_not_available() -> HTTPException:
    return error(403, CONTEXT_NOT_AVAILABLE, "You do not hold that role there.")


def format_context(ctx: Context) -> str:
    """``<jurisdiction|->:<role>``: the value of X-Requested-Context."""
    return f"{ctx.jurisdiction or GLOBAL}:{ctx.role}"


def parse_requested_context(value: str | None) -> Context | None:
    """The context named by X-Requested-Context, or None if the header is absent or not of
    the form ``<jurisdiction|->:<role>`` (the caller then treats it as not held)."""
    if not value or ":" not in value:
        return None
    jurisdiction, _, role = value.strip().rpartition(":")
    jurisdiction, role = jurisdiction.strip(), role.strip()
    if not jurisdiction or not role:
        return None
    return Context(jurisdiction=None if jurisdiction == GLOBAL else jurisdiction.upper(), role=role)


def check_advisory_headers(request: Request, ctx: Context | None) -> None:
    """409 ``context_changed`` if the tab's X-Jurisdiction or X-Role, when sent, differ
    from the token's context. Absent headers are fine; so is no context (nothing to
    differ from: the route refuses it, or does not need one)."""
    if ctx is None:
        return
    jurisdiction = request.headers.get(HEADER_JURISDICTION)
    if jurisdiction is not None:
        wanted = None if jurisdiction.strip() == GLOBAL else jurisdiction.strip().upper()
        if wanted != ctx.jurisdiction:
            raise context_changed()
    role = request.headers.get(HEADER_ROLE)
    if role is not None and role.strip() != ctx.role:
        raise context_changed()


def context_of(user: object) -> Context | None:
    """The (jurisdiction, role) the BenchNotes token says ``user`` acts in, as the auth dependency
    attached it (``AuthenticatedUser.ctx``); None until chosen. The one place that reads it:
    the permission checks (authorisation.py) and the jurisdiction lookup (jurisdictions.py) both
    come here, and tests replace it."""
    return getattr(user, "ctx", None)


def sysadmin_of(user: object) -> bool:
    """Whether the token says ``user`` is a SystemAdministrator (``AuthenticatedUser.sysadmin``)."""
    return bool(getattr(user, "sysadmin", False))


@dataclass(frozen=True)
class AuditContext:
    """The context a row is stamped with: the active jurisdiction (its ID: None for a
    global role or where it could not be resolved) and role."""

    jurisdiction_id: UUID | None
    role: str | None


_audit_context: ContextVar[AuditContext | None] = ContextVar("audit_context", default=None)


def set_audit_context(context: AuditContext | None) -> None:
    """Set by the auth dependency, for the rest of the request (and the audit rows
    written in it)."""
    _audit_context.set(context)


def audit_context_fields() -> dict[str, object]:
    """``active_jurisdiction_id`` and ``active_role`` for an audit_log row: those of the
    request being served, or nulls (no request, no context chosen, a worker)."""
    context = _audit_context.get()
    if context is None:
        return {"active_jurisdiction_id": None, "active_role": None}
    return {"active_jurisdiction_id": context.jurisdiction_id, "active_role": context.role}
