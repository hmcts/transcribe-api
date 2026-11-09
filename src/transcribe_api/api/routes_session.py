"""Sign-in and the user's own context (DIAAT-373, DIAAT-386, DIAAT-390), under ``/api``.

``POST /api/auth/session`` is the one route that accepts an Entra ID token (or, locally, a
dev persona): it returns the BenchNotes access token every other route then requires.
The rest work for any signed-in user, with or without a context chosen:

* ``GET /api/me/permissions``: who am I, what do I hold, what am I acting as and may do;
* ``PUT /api/me/context``: choose the (jurisdiction, role) to act as (403 for one not held);
* ``DELETE /api/me/jurisdictions/{code}``: leave a jurisdiction (audited ``membership.leave``);
* ``GET /api/jurisdictions/public``: the enabled jurisdictions, for the access request form.

Each token-returning route answers ``{token, expires_at, me}``: the web app keeps the token
in its server-side session, never in the browser. Plain defs: the database work is
synchronous, so FastAPI runs them in the threadpool.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, StringConstraints
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from transcribe_api.domain.assignments import distinct_contexts, leave_jurisdiction, load_assignments
from transcribe_api.domain.audit import record_audit
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.authorisation import sign_in_endpoint
from transcribe_api.domain.auth.context import context_not_available, error, parse_requested_context
from transcribe_api.domain.auth.dependencies_dictation import signed_in_endpoint
from transcribe_api.domain.auth.dev_personas import PERSONAS
from transcribe_api.domain.auth.permissions import REQUESTABLE_ROLES, ROLE_SYSTEM_ADMIN
from transcribe_api.domain.auth.sessions import (
    JurisdictionRef,
    MeResponse,
    SessionResponse,
    SignInIdentity,
    build_me,
    mint_session,
    sign_in,
)
from transcribe_api.domain.auth.token_auth import _is_local_dev, get_entra_identity
from transcribe_api.domain.models_dictation import Jurisdiction, User
from transcribe_api.runtime.db_connection import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["session"])


async def get_sign_in_identity(
    request: Request,
    x_dev_persona: Annotated[str | None, Header()] = None,
) -> SignInIdentity:
    """Who is signing in: an Entra ID token, or, in local mode only, a dev persona.

    The persona header is refused (401) anywhere but ENVIRONMENT=local, whatever else the
    request carries: it names a user without proving anything."""
    if x_dev_persona is not None:
        if not _is_local_dev():
            raise HTTPException(
                status_code=401,
                detail={"code": "dev_persona_disabled", "message": "Dev personas are only available locally."},
                headers={"WWW-Authenticate": "Bearer"},
            )
        persona = PERSONAS.get(x_dev_persona.strip())
        if persona is None:
            raise error(400, "unknown_persona", f"Unknown persona. Known: {', '.join(sorted(PERSONAS))}.")
        # The user must be there (scripts-seed-personas.py) for one with assignments: they
        # are the point of the persona, and read from the database like anyone's.
        if persona.assignments and not await run_in_threadpool(_persona_is_seeded, persona.azure_user_id):
            raise error(
                409,
                "persona_not_seeded",
                "Run scripts-seed-personas.py to create the dev personas and their assignments.",
            )
        return SignInIdentity(
            azure_user_id=persona.azure_user_id, email=persona.email, name=persona.display_name, sysadmin=persona.sysadmin
        )

    entra = await get_entra_identity(request, request.headers.get("authorization"))
    return SignInIdentity(
        azure_user_id=entra.user_id,
        email=entra.email,
        name=entra.name,
        # Only the Entra roles claim says so, at sign-in; the database never does. The role's
        # value in the token is its name (the library's configurable role map is not used).
        sysadmin=ROLE_SYSTEM_ADMIN in entra.roles,
    )


def _persona_is_seeded(azure_user_id: str) -> bool:
    with Session(get_engine()) as session:
        return session.exec(select(User.id).where(User.azure_user_id == azure_user_id)).first() is not None


def _no_store(response: Response) -> None:
    # A token, or what this user holds right now: never cached.
    response.headers["Cache-Control"] = "no-store"


@router.post("/auth/session", response_model=SessionResponse, dependencies=[Depends(sign_in_endpoint)])
def create_session(
    response: Response,
    identity: Annotated[SignInIdentity, Depends(get_sign_in_identity)],
    x_requested_context: Annotated[str | None, Header()] = None,
) -> SessionResponse:
    """Sign in with an Entra ID token (or a local dev persona) and get a BenchNotes access
    token. Also how the web app renews it before it expires: it sends the context its
    session chose as ``X-Requested-Context: <jurisdiction|->:<role>``, kept only if the
    user still holds it.

    The user is created at their first sign-in, with no role (what was assigned to their
    email beforehand is theirs). 403 ``user_deactivated`` for a deactivated user."""
    _no_store(response)
    with Session(get_engine()) as session:
        user = sign_in(session, azure_user_id=identity.azure_user_id, email=identity.email, name=identity.name)
        return mint_session(
            session,
            user,
            sysadmin=identity.sysadmin,
            requested=parse_requested_context(x_requested_context),
            requested_given=x_requested_context is not None,
        )


@router.get("/me/permissions", response_model=MeResponse)
def my_permissions(response: Response, current_user: User = Depends(signed_in_endpoint)) -> MeResponse:
    """Who the signed-in user is, what they hold (read now), what they are acting as (the
    token's context) and the permissions of that role, plus the system ones for a
    SystemAdministrator. Works with no context chosen, and for a user with no role."""
    _no_store(response)
    with Session(get_engine()) as session:
        return build_me(session, current_user.db_user, sysadmin=current_user.sysadmin, ctx=current_user.ctx)


class ContextBody(BaseModel):
    # null: the global JurisdictionSuperAdministrator. Required, so that leaving it out is not
    # taken to mean that.
    jurisdiction: Annotated[str, StringConstraints(strip_whitespace=True, max_length=16)] | None
    role: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]


@router.put("/me/context", response_model=SessionResponse)
def set_context(
    body: ContextBody, response: Response, current_user: User = Depends(signed_in_endpoint)
) -> SessionResponse:
    """Act as ``role`` in ``jurisdiction`` from now on: a new token with that context. 403
    ``context_not_available`` for a pair the user does not hold (SystemAdministrator is never
    one); nothing changes."""
    _no_store(response)
    wanted = Context(jurisdiction=body.jurisdiction.upper() if body.jurisdiction else None, role=body.role)
    with Session(get_engine()) as session:
        held = distinct_contexts(load_assignments(session, current_user.id))
        if wanted not in held:
            raise context_not_available()
        return mint_session(session, current_user.db_user, sysadmin=current_user.sysadmin, chosen=wanted)


@router.delete("/me/jurisdictions/{code}", response_model=SessionResponse)
def leave(code: str, response: Response, current_user: User = Depends(signed_in_endpoint)) -> SessionResponse:
    """Leave a jurisdiction: the user's roles there are removed (and nobody else's), audited as
    ``membership.leave``. If they were acting in it they must choose again (or, with one pair
    left, it is selected); any other context is kept. 404 ``not_a_member`` if they held no role there."""
    _no_store(response)
    code = code.strip().upper()
    with Session(get_engine()) as session:
        left = leave_jurisdiction(session, current_user.id, code)
        if not left.roles:
            raise error(404, "not_a_member", "You do not hold a role in that jurisdiction.")
        session.commit()
        # Audited in the context the user acted in when they left (the request's).
        record_audit(
            "membership.leave",
            actor_id=current_user.id,
            outcome="success",
            object_type="jurisdiction",
            object_id=left.jurisdiction_id,
            detail={"code": code, "roles": left.roles},
        )
        previous = current_user.ctx
        keep = previous is not None and previous.jurisdiction != code
        return mint_session(
            session,
            current_user.db_user,
            sysadmin=current_user.sysadmin,
            requested=previous if keep else None,
            requested_given=keep,
        )


class PublicJurisdiction(JurisdictionRef):
    # The roles a user may ask for there (never an administrator role): for the access request form.
    roles: list[str]


@router.get("/jurisdictions/public", response_model=list[PublicJurisdiction])
def public_jurisdictions(response: Response, _user: User = Depends(signed_in_endpoint)) -> list[PublicJurisdiction]:
    """Every enabled jurisdiction, by name, with the roles that can be requested in it: what a
    user with no role chooses from on the access request form. Any signed-in user, with or
    without a role or a context."""
    response.headers["Cache-Control"] = "no-store"
    with Session(get_engine()) as session:
        rows = session.exec(select(Jurisdiction).where(Jurisdiction.enabled).order_by(Jurisdiction.name)).all()
    return [PublicJurisdiction(code=row.code, name=row.name, roles=list(REQUESTABLE_ROLES)) for row in rows]

