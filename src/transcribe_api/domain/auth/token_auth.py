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
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException
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

_UNAUTHORISED_HEADERS = {"WWW-Authenticate": "Bearer"}


def _is_local_dev() -> bool:
    return os.getenv("ENVIRONMENT", "production").lower() == "local"


def _unauthorised(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers=_UNAUTHORISED_HEADERS)


async def get_token_auth_user(
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
