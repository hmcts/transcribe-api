"""Authenticate API callers: Entra ID at sign-in, the BenchNotes token everywhere else.

DIAAT-373 splits authentication in two:

* ``POST /api/auth/session`` is the only route that accepts an Entra ID token
  (``get_entra_identity``). It checks the token, finds or creates the user, and returns a
  short-lived BenchNotes access token (domain/auth/app_token.py, domain/auth/sessions.py).
  Locally (ENVIRONMENT=local) it also accepts a dev persona instead (``X-Dev-Persona``).
* Every other route accepts only that BenchNotes token (``get_token_auth_user``,
  ``build_token_user_dep``): its signature, issuer, audience and expiry are checked locally,
  and what it says about the user (who they are, SystemAdministrator or not, their
  assignments, the context they act in) is trusted for its lifetime, so authorising a
  request reads nothing from the database. An Entra token is refused on those routes, and
  a BenchNotes token on the sign-in.

Why this does not use hmcts-fastapi-azure-auth's own dependency
---------------------------------------------------------------
The library's dependency requires the `X-Ms-Client-Principal` header, which only
Azure App Service Easy Auth injects, and takes the caller's email and name from
it. On the CNP platform (AKS) there is no Easy Auth: nothing would ever set the
header legitimately, so every request would be refused — and anything that did
reach the app could set the header itself. Combined with the dictation
resolver creating a user's DB row with that email on first login, and the blob
ownership check accepting a match on email, a caller could have claimed
another user's email.

So identity here comes only from the token. This mirrors the CNP reference for
internal users (DARTS): the frontend runs the OIDC login and keeps tokens in a
server-side session, and the API validates the bearer JWT it is sent. The
library's verifier is reused unchanged for the Entra token — signature against
Entra's JWKS, audience (our client ID), issuer (our tenant), expiry — and so is
its role check, for the sign-in gate below only: what a user may do after sign-in is
decided by the permission checks (domain/auth/authorisation.py), not by Entra roles.

One deliberate tightening: the library's verifier returns None, rather than
raising, when verification is disabled or strict mode is off. Here None is
always a 401. There is no path on which an unverified token authenticates.

App access roles
----------------
When AUTH_ACCESS_ROLES is set (a comma-separated list, e.g.
"Judge,LegalTextManager,SystemAdministrator"), a caller holding none of
those Entra app roles is refused with 403 at sign-in, before any application user
is resolved or created. It backs up "Assignment required" on the app
registration: if that were ever switched off, any user in the tenant could get
a token. The check is the library's own role check, so the 403 and the
UNAUTHORISED_ACCESS_ATTEMPT log line match every other role-gated route.
Unset or blank: not checked. DIAAT-386 empties it in each environment once the
permissions are enforced, so that a tenant member with no role can sign in and
see "no access yet". The setting and this check still exist (they are removed with the Entra
role groups: DIAAT-386/DIAAT-387, Phase 8: a configuration change, not code).

PR previews
-----------
PR previews run the web app with sign-in off (Entra cannot redirect to per-PR hosts), so
no Entra token is sent. With AUTH_PREVIEW_USER_ENABLED and ENVIRONMENT=preview, a sign-in
(``POST /api/auth/session``) with no Authorization header is the fixed preview user,
holding AUTH_PREVIEW_USER_ROLES (default: every app role, so SystemAdministrator too);
those roles still go through the AUTH_ACCESS_ROLES check, and a sign-in that does send a
token is verified as usual. The preview user is given every role in every jurisdiction at
sign-in (domain/auth/sessions.py), and is then in the same position as anyone else: it has
to sign in to get a BenchNotes token, which every other route requires. Settings refuse to
load with the flag on in any other environment, and the check here repeats that condition.
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request
from hmcts_azure_auth import get_allowlisted_user
from hmcts_azure_auth.jwt import get_jwt_service
from hmcts_azure_auth.models import AuthUser
from hmcts_azure_auth.roles import get_valid_roles
from starlette.concurrency import run_in_threadpool

from transcribe_api.domain.auth import app_token
from transcribe_api.domain.auth.app_token import AppPrincipal, AppTokenError
from transcribe_api.domain.auth.context import (
    AuditContext,
    check_advisory_headers,
    context_required,
    set_audit_context,
)

logger = logging.getLogger(__name__)

PREVIEW_USER_ID = "preview-user"
PREVIEW_USER_NAME = "Preview User"
PREVIEW_USER_EMAIL = "preview-user@preview.platform.hmcts.net"

_UNAUTHORISED_HEADERS = {"WWW-Authenticate": "Bearer"}


def _is_local_dev() -> bool:
    """ENVIRONMENT is ``local`` (without regard to case or surrounding space, as the settings read it).
    Unset is not local."""
    return os.getenv("ENVIRONMENT", "production").strip().lower() == "local"


def preview_user_enabled() -> bool:
    flag = os.getenv("AUTH_PREVIEW_USER_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    return flag and os.getenv("ENVIRONMENT", "").strip().lower() == "preview"


def _roles_from_env(name: str) -> list[str]:
    return [r.strip() for r in os.getenv(name, "").split(",") if r.strip()]


def _access_roles() -> list[str]:
    return _roles_from_env("AUTH_ACCESS_ROLES")


def _preview_user() -> AuthUser:
    return AuthUser(
        user_id=PREVIEW_USER_ID,
        name=PREVIEW_USER_NAME,
        email=PREVIEW_USER_EMAIL,
        roles=_roles_from_env("AUTH_PREVIEW_USER_ROLES") or list(get_valid_roles().values()),
    )


NOT_AUTHENTICATED = "not_authenticated"
INVALID_TOKEN = "invalid_token"


def _unauthorised(message: str, code: str = INVALID_TOKEN) -> HTTPException:
    """The 401, in the API's error shape ``{"detail": {"code", "message"}}``, with the challenge."""
    return HTTPException(status_code=401, detail={"code": code, "message": message}, headers=_UNAUTHORISED_HEADERS)


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise _unauthorised("Bearer token required", NOT_AUTHENTICATED)
    token = authorization[len("Bearer ") :].strip()
    if not token:
        raise _unauthorised("Bearer token required", NOT_AUTHENTICATED)
    return token


async def get_entra_identity(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthUser:
    """The caller's identity, taken solely from a verified Entra ID bearer token (in a PR preview
    with the preview user enabled, and no token sent, the fixed preview user). Only the sign-in
    (POST /api/auth/session) uses it."""
    if authorization is None and preview_user_enabled():
        user = _preview_user()
    else:
        user = await _verified_entra_user(authorization)

    access_roles = _access_roles()
    if access_roles:
        await get_allowlisted_user(required_roles_any=access_roles)(request, user)
    return user


async def _verified_entra_user(authorization: str | None) -> AuthUser:
    token = _bearer(authorization)

    service = get_jwt_service()
    decoded = await service.verify_jwt_token(token)
    if not decoded:
        # Verification disabled, or a non-strict failure that the library
        # downgraded to None. Either way the token is not verified.
        logger.warning("Rejected a bearer token that could not be verified")
        raise _unauthorised("Bearer token could not be verified")

    claims = service.extract_user_info_from_jwt(decoded)
    user_id = claims.get("azure_user_id") or ""
    if not user_id:
        raise _unauthorised("Bearer token has no oid claim")

    return AuthUser(
        user_id=user_id,
        name=claims.get("name") or "",
        email=claims.get("email") or "",
        roles=claims.get("roles") or [],
    )


async def get_token_auth_user(
    authorization: Annotated[str | None, Header()] = None,
) -> AppPrincipal:
    """What a verified BenchNotes access token says about the caller. 401 for anything
    else (an Entra token, an expired or tampered one, none)."""
    token = _bearer(authorization)
    try:
        return app_token.verify(token)
    except AppTokenError:
        logger.warning("Rejected a bearer token that is not a valid BenchNotes access token")
        raise _unauthorised("Bearer token is not a valid access token") from None


def build_token_user_dep(
    user_resolver: Callable[[AppPrincipal], Any], *, require_context: bool = True
) -> Callable:
    """Dependency factory: a verified BenchNotes token -> the application's user object.

    ``user_resolver(principal)`` loads the DB user from the token's ``sub`` and wraps it
    with the token's assignments and context (dictation and recording each have their own).
    A synchronous resolver (they open a DB session) is run in the threadpool so it does
    not block the event loop.

    ``require_context``: the route needs the user to be acting in a context, so with none
    chosen it is 403 ``context_required`` (a SystemAdministrator is let through: their
    system-level permissions do not depend on a context, and what else they may do is the
    route's own check). And a tab's advisory X-Jurisdiction and X-Role that differ from the
    token's context are 409 ``context_changed``. The no-context endpoints (domain/auth/
    dependencies_session.py) do not require one.
    """

    async def _dep(request: Request, principal: Annotated[AppPrincipal, Depends(get_token_auth_user)]) -> Any:
        if require_context:
            if principal.ctx is None and not principal.sysadmin:
                raise context_required()
            check_advisory_headers(request, principal.ctx)
        if inspect.iscoroutinefunction(user_resolver):
            user = await user_resolver(principal)
        else:
            user = await run_in_threadpool(user_resolver, principal)
        await _stamp_audit_context(principal)
        return user

    return _dep


async def _stamp_audit_context(principal: AppPrincipal) -> None:
    """Audit rows written in this request carry the context it acts in (record_audit)."""
    from transcribe_api.domain.jurisdictions import jurisdiction_id_for_code, known_jurisdiction_id

    ctx = principal.ctx
    if ctx is None:
        set_audit_context(None)
        return
    jurisdiction_id = None
    if ctx.jurisdiction is not None:
        jurisdiction_id = known_jurisdiction_id(ctx.jurisdiction) or await run_in_threadpool(
            jurisdiction_id_for_code, ctx.jurisdiction
        )
    set_audit_context(AuditContext(jurisdiction_id=jurisdiction_id, role=ctx.role))
