"""Sign-in and the BenchNotes session (DIAAT-373, DIAAT-382, DIAAT-386, DIAAT-390).

``sign_in`` finds or creates the user for a verified identity. ``mint_session`` reads their
assignments, picks the context they act in, signs the BenchNotes access token
(domain/auth/app_token.py) and builds the ``MeResponse`` that goes with it: who they are,
what they hold, what they are acting as, and what that lets them do.

Choosing the context (DIAAT-390): there is no default. Exactly one distinct
(jurisdiction, role) pair, and nothing else, is selected automatically; with several the
context stays null until the user chooses (``PUT /api/me/context``); with none the user has
"no access yet". A re-mint carries the session's context over (``X-Requested-Context``),
but only if the user still holds it: otherwise it is null and they choose again.
SystemAdministrator is never an assignment or a context: it is the ``sysadmin`` flag, copied
from the Entra token at sign-in, and gives system-level permissions whatever the context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlmodel import Session, select

from transcribe_api.domain.assignments import Assignment, distinct_contexts, load_assignments
from transcribe_api.domain.auth import app_token
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.permissions import permissions_for
from transcribe_api.domain.auth.user_creation import insert_user_or_load_existing
from transcribe_api.domain.auth.user_loading import user_deactivated
from transcribe_api.domain.models_dictation import AccessRequest, AccessRequestRole, Jurisdiction, User

logger = logging.getLogger(__name__)


class JurisdictionRef(BaseModel):
    code: str
    name: str


class UserOut(BaseModel):
    id: UUID
    email: str
    name: str


class AssignmentOut(BaseModel):
    jurisdiction: JurisdictionRef | None
    role: str


class AccessRequestOut(BaseModel):
    id: UUID
    jurisdiction: JurisdictionRef
    roles: list[str]
    status: Literal["pending", "approved", "rejected", "withdrawn"]
    created_at: datetime
    decided_at: datetime | None
    reason: str | None


class MeResponse(BaseModel):
    user: UserOut
    # ready: acting in a context (or a SystemAdministrator with no assignments: system pages
    # only); choose: has assignments but has not chosen; no_access: nothing to act as.
    status: Literal["ready", "choose", "no_access"]
    assignments: list[AssignmentOut]
    active: AssignmentOut | None
    permissions: list[str]
    pending_requests: list[AccessRequestOut]


class SessionResponse(BaseModel):
    token: str
    expires_at: datetime
    me: MeResponse


# --- sign-in ---------------------------------------------------------------------------


def sign_in(session: Session, *, azure_user_id: str, email: str, name: str) -> User:
    """The user for a verified identity, created at their first sign-in (with what was
    assigned to their email beforehand: domain/jurisdictions.enrol_new_user) and given no
    role of their own.

    403 ``user_deactivated`` for a deactivated user: signing in never re-creates or re-enables
    them (their row is found by the same ID, and stays as it is)."""
    user = session.exec(select(User).where(User.azure_user_id == azure_user_id)).first()
    if user is None:
        user = insert_user_or_load_existing(
            session, User(email=email, azure_user_id=azure_user_id, name=name or None)
        )
    elif name and user.name != name:
        user.name = name
        session.add(user)
        session.commit()
        session.refresh(user)
    if not user.is_active:
        raise user_deactivated()
    return user


# --- the context -----------------------------------------------------------------------


def select_context(contexts: list[Context], *, requested: Context | None, requested_given: bool) -> Context | None:
    """The context a minted token carries. ``requested_given``: the caller named one (even
    one that makes no sense): it is kept if the user holds it, and otherwise there is none.
    Not named: the single pair, if there is exactly one."""
    if requested_given:
        return requested if requested is not None and requested in contexts else None
    return contexts[0] if len(contexts) == 1 else None


def _out(assignment: Assignment) -> AssignmentOut:
    return AssignmentOut(
        jurisdiction=(
            JurisdictionRef(code=assignment.jurisdiction_code, name=assignment.jurisdiction_name)
            if assignment.jurisdiction_code is not None
            else None
        ),
        role=assignment.role,
    )


def _active_out(ctx: Context | None, assignments: list[Assignment], session: Session) -> AssignmentOut | None:
    if ctx is None:
        return None
    for assignment in assignments:
        if assignment.context == ctx:
            return _out(assignment)
    if ctx.jurisdiction is None:
        return AssignmentOut(jurisdiction=None, role=ctx.role)
    # The token's context is no longer one of their assignments (removed since it was
    # minted): still what this token acts as, until it is replaced.
    name = session.exec(select(Jurisdiction.name).where(Jurisdiction.code == ctx.jurisdiction)).first()
    return AssignmentOut(jurisdiction=JurisdictionRef(code=ctx.jurisdiction, name=name or ctx.jurisdiction), role=ctx.role)


def pending_requests(session: Session, user_id: UUID) -> list[AccessRequestOut]:
    """The user's access requests that are still pending, oldest first."""
    rows = session.exec(
        select(AccessRequest, Jurisdiction)
        .join(Jurisdiction, Jurisdiction.id == AccessRequest.jurisdiction_id)
        .where(AccessRequest.user_id == user_id, AccessRequest.status == "pending")
        .order_by(AccessRequest.created_at)
    ).all()
    out = []
    for request, jurisdiction in rows:
        roles = sorted(
            session.exec(select(AccessRequestRole.role).where(AccessRequestRole.access_request_id == request.id)).all()
        )
        out.append(
            AccessRequestOut(
                id=request.id,
                jurisdiction=JurisdictionRef(code=jurisdiction.code, name=jurisdiction.name),
                roles=roles,
                status=request.status,
                created_at=request.created_at,
                decided_at=request.decided_at,
                reason=request.reason,
            )
        )
    return out


def build_me(
    session: Session,
    user: User,
    *,
    sysadmin: bool,
    ctx: Context | None,
    assignments: list[Assignment] | None = None,
) -> MeResponse:
    """The ``MeResponse``: the user's assignments (read now), the context given (the token's),
    and the permissions of that context's role plus the system ones for a SystemAdministrator."""
    if assignments is None:
        assignments = load_assignments(session, user.id)
    if ctx is not None:
        status = "ready"
    elif assignments:
        status = "choose"
    elif sysadmin:
        status = "ready"  # system pages only
    else:
        status = "no_access"
    return MeResponse(
        user=UserOut(id=user.id, email=user.email, name=user.name or user.email),
        status=status,
        assignments=[_out(assignment) for assignment in assignments],
        active=_active_out(ctx, assignments, session),
        permissions=permissions_for(ctx.role if ctx else None, sysadmin),
        pending_requests=pending_requests(session, user.id),
    )


def mint_session(
    session: Session,
    user: User,
    *,
    sysadmin: bool,
    requested: Context | None = None,
    requested_given: bool = False,
    chosen: Context | None = None,
) -> SessionResponse:
    """Read the user's assignments, pick the context, sign a token, and say who they are.

    ``chosen``: a context the caller has already checked against the assignments (the user
    asked for it: PUT /api/me/context). Otherwise it is ``requested`` / the auto rule
    (``select_context``)."""
    assignments = load_assignments(session, user.id)
    contexts = distinct_contexts(assignments)
    ctx = chosen if chosen is not None else select_context(contexts, requested=requested, requested_given=requested_given)
    token, expires_at = app_token.mint(
        user.id,
        sysadmin=sysadmin,
        assignments=contexts,
        ctx=ctx,
        name=user.name or "",
    )
    return SessionResponse(
        token=token,
        expires_at=expires_at,
        me=build_me(session, user, sysadmin=sysadmin, ctx=ctx, assignments=assignments),
    )


@dataclass(frozen=True)
class SignInIdentity:
    """A verified identity at sign-in: from an Entra ID token, or a local dev persona."""

    azure_user_id: str
    email: str
    name: str
    sysadmin: bool
