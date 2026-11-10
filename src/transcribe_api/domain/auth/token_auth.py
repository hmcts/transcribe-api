"""Authenticate API callers from a verified Entra ID bearer token, and nothing else.

Why this exists instead of hmcts-fastapi-azure-auth's own dependency
---------------------------------------------------------------------
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
library's verifier is reused unchanged — signature against Entra's JWKS,
audience (our client ID), issuer (our tenant), expiry — and so are its role
checks (get_allowlisted_user accepts our dependency via current_user_dep).

One deliberate tightening: the library's verifier returns None, rather than
raising, when verification is disabled or strict mode is off. Here None is
always a 401. There is no path on which an unverified token authenticates.

App access roles
----------------
When AUTH_ACCESS_ROLES is set (a comma-separated list, e.g.
"Judge,LegalTextManager,SystemAdministrator,Normal"), a caller holding none of
those app roles is refused with 403 here, before any application user is
resolved or created. It backs up "Assignment required" on the app
registration: if that were ever switched off, any user in the tenant could get
a token. The check is the library's own role check, so the 403 and the
UNAUTHORISED_ACCESS_ATTEMPT log line match every other role-gated route.
Unset or blank: not checked.

PR previews
-----------
PR previews run the web app with sign-in off (Entra cannot redirect to per-PR
hosts), so the API is called without a token. With AUTH_PREVIEW_USER_ENABLED
and ENVIRONMENT=preview, a request with no Authorization header authenticates
as a fixed preview user holding AUTH_PREVIEW_USER_ROLES (default: every app
role). Those roles still go through the AUTH_ACCESS_ROLES check, and a request
that does send a token is verified as usual. Settings refuse to load with the
flag on in any other environment, and the check here repeats that condition.
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

logger = logging.getLogger(__name__)

# Same identity the library uses for local development, so DB rows created by
# local runs before this change still resolve to the same user.
LOCAL_DEV_USER_ID = "local-dev-user-123"
LOCAL_DEV_NAME = "Local Developer"
LOCAL_DEV_EMAIL = "developer@localhost.com"

PREVIEW_USER_ID = "preview-user"
PREVIEW_USER_NAME = "Preview User"
PREVIEW_USER_EMAIL = "preview-user@preview.platform.hmcts.net"

_UNAUTHORISED_HEADERS = {"WWW-Authenticate": "Bearer"}


def _is_local_dev() -> bool:
    return os.getenv("ENVIRONMENT", "production").lower() == "local"


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


def _unauthorised(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers=_UNAUTHORISED_HEADERS)


async def get_token_auth_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthUser:
    """Return the caller's identity, taken solely from a verified bearer token."""
    if _is_local_dev():
        return AuthUser(
            user_id=LOCAL_DEV_USER_ID,
            name=LOCAL_DEV_NAME,
            email=LOCAL_DEV_EMAIL,
            roles=list(get_valid_roles().values()),
        )

    if authorization is None and preview_user_enabled():
        user = _preview_user()
    else:
        user = await _verified_token_user(authorization)

    access_roles = _access_roles()
    if access_roles:
        await get_allowlisted_user(required_roles_any=access_roles)(request, user)
    return user


async def _verified_token_user(authorization: str | None) -> AuthUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise _unauthorised("Bearer token required")
    token = authorization[len("Bearer ") :].strip()
    if not token:
        raise _unauthorised("Bearer token required")

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


def build_token_user_dep(user_resolver: Callable[[str, str, list[str]], Any]) -> Callable:
    """Dependency factory: verified token identity -> the application's user object.

    Equivalent to the library's build_current_user_dep, with the token as the
    only source of identity. A synchronous resolver (they open a DB session) is
    run in the threadpool so it does not block the event loop.
    """

    async def _dep(auth_user: Annotated[AuthUser, Depends(get_token_auth_user)]) -> Any:
        args = (auth_user.user_id, auth_user.email, auth_user.roles)
        if inspect.iscoroutinefunction(user_resolver):
            return await user_resolver(*args)
        return await run_in_threadpool(user_resolver, *args)

    return _dep
