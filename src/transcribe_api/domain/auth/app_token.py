"""The BenchNotes access token (DIAAT-373, DIAAT-382, DIAAT-390).

``POST /api/auth/session`` verifies an Entra ID token (or, locally only, a dev persona),
finds the user and their role assignments, and returns this short-lived token. Every
other route accepts only this token: its signature, audience, issuer and expiry are
checked locally, and what it says (who the user is, whether they are a
SystemAdministrator, which (jurisdiction, role) pairs they hold, which one they are
acting in) is trusted for the lifetime of the token. No database read is needed to
authorise a request, so a change to a user's assignments reaches them at the next mint
(``APP_JWT_TTL_SECONDS``, 300 by default).

HS256, signed with ``APP_JWT_SIGNING_KEY``. ``iss`` is ``benchnotes-api`` and ``aud`` is
``benchnotes``, so an Entra token (RS256, another issuer and audience) can never pass
as one, nor this as an Entra token.

Claims::

    sub          the user's UUID
    iat, exp     issued at, expires (seconds)
    sess_exp     when the Entra-verified sign-in this token descends from stops being good
                 (seconds): sign-in time + one lifetime. A token minted at sign-in has
                 ``exp == sess_exp``; one minted without Entra (``PUT /api/me/context``,
                 ``DELETE /api/me/jurisdictions/{code}``) carries the presenting token's
                 ``sess_exp`` and expires no later than it, so re-minting never extends the
                 ``sysadmin`` claim beyond one lifetime after the last Entra verification
    sysadmin     true when the Entra ``roles`` claim held SystemAdministrator at mint
                 time; never read from the database
    assignments  [{"jurisdiction": "ET" | null, "role": "Judge"}, ...]
    ctx          {"jurisdiction": "ET" | null, "role": "Judge"}, or null until chosen
    name         display name (informational; never used to authorise)

Permissions are not in the token: they are derived from ``ctx.role`` and ``sysadmin``
(domain/auth/permissions.py).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import jwt

from transcribe_api.domain.auth.permissions import STORED_ROLES
from transcribe_api.runtime.settings_dictation import get_settings

logger = logging.getLogger(__name__)

ISSUER = "benchnotes-api"
AUDIENCE = "benchnotes"
ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 300
# HS256 wants a key as long as its hash (RFC 7518 3.2).
MIN_KEY_LENGTH = 32

_ENVIRONMENTS_WITHOUT_A_REQUIRED_KEY = ("local", "test")


class AppTokenError(Exception):
    """The token is not a valid BenchNotes access token (the caller answers 401)."""


@dataclass(frozen=True)
class Context:
    """A (jurisdiction code, role) pair: an assignment, or the context a user acts in.
    ``jurisdiction`` is None for the global JurisdictionSuperAdministrator."""

    jurisdiction: str | None
    role: str

    def to_claim(self) -> dict[str, str | None]:
        return {"jurisdiction": self.jurisdiction, "role": self.role}

    @classmethod
    def from_claim(cls, claim: object) -> Context:
        if not isinstance(claim, dict):
            msg = "a context is an object"
            raise AppTokenError(msg)
        jurisdiction, role = claim.get("jurisdiction"), claim.get("role")
        if jurisdiction is not None and not isinstance(jurisdiction, str):
            msg = "context.jurisdiction must be a string or null"
            raise AppTokenError(msg)
        if role not in STORED_ROLES:
            msg = "context.role is not a stored role"
            raise AppTokenError(msg)
        return cls(jurisdiction=jurisdiction, role=role)


@dataclass(frozen=True)
class AppPrincipal:
    """What a verified BenchNotes token says about the caller."""

    user_id: UUID
    sysadmin: bool
    assignments: tuple[Context, ...]
    ctx: Context | None
    issued_at: datetime
    expires_at: datetime
    name: str = ""
    # Where the chain of re-mints back to the last Entra-verified sign-in ends (see ``mint``).
    session_expires_at: datetime | None = None


_ephemeral_key: str | None = None


def signing_key_problem(environment: str, key: str | None) -> str | None:
    """Why ``key`` is unusable in ``environment`` (None if it is fine). Outside local and
    test it is required, and long enough for HS256."""
    if environment in _ENVIRONMENTS_WITHOUT_A_REQUIRED_KEY:
        return None
    if not key:
        return "APP_JWT_SIGNING_KEY is not set"
    if len(key) < MIN_KEY_LENGTH:
        return f"APP_JWT_SIGNING_KEY must be at least {MIN_KEY_LENGTH} characters"
    return None


def _signing_key() -> str:
    settings = get_settings()
    key = settings.APP_JWT_SIGNING_KEY
    if key:
        return key
    if settings.ENVIRONMENT not in _ENVIRONMENTS_WITHOUT_A_REQUIRED_KEY:
        msg = "APP_JWT_SIGNING_KEY is not set"
        raise RuntimeError(msg)
    # Local runs without a key in .env: one for this process, so sign-in still works. Tokens
    # do not survive a restart.
    global _ephemeral_key
    if _ephemeral_key is None:
        _ephemeral_key = secrets.token_urlsafe(48)
        logger.warning("APP_JWT_SIGNING_KEY is not set: using a key for this process only (local)")
    return _ephemeral_key


def derived_key(purpose: str) -> bytes:
    """A key for ``purpose`` derived from the signing key (HMAC-SHA256), so something else that needs
    a signature (the signed local-audio URLs) never signs with the token key itself."""
    return hmac.new(_signing_key().encode(), f"benchnotes:{purpose}".encode(), hashlib.sha256).digest()


def ttl_seconds() -> int:
    ttl = get_settings().APP_JWT_TTL_SECONDS
    return ttl if ttl > 0 else DEFAULT_TTL_SECONDS


def mint(
    user_id: UUID | str,
    *,
    sysadmin: bool,
    assignments: list[Context] | tuple[Context, ...],
    ctx: Context | None,
    name: str = "",
    now: float | None = None,
    session_expires_at: datetime | None = None,
) -> tuple[str, datetime]:
    """Sign a token. Returns it with its expiry.

    ``session_expires_at``: given when the token is minted from another BenchNotes token and not
    from a verified Entra token (a context change): the presenting token's ``sess_exp``. The new
    token never outlives it, so a chain of re-mints is over one lifetime after the Entra
    verification it started from (and the ``sysadmin`` claim with it). Left out (a sign-in), the
    session starts now."""
    issued = int(now if now is not None else time.time())
    expires = issued + ttl_seconds()
    sess_exp = expires if session_expires_at is None else int(session_expires_at.timestamp())
    expires = min(expires, sess_exp)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "iat": issued,
        "exp": expires,
        "sess_exp": sess_exp,
        "sysadmin": bool(sysadmin),
        "assignments": [assignment.to_claim() for assignment in assignments],
        "ctx": ctx.to_claim() if ctx is not None else None,
        "name": name,
    }
    token = jwt.encode(claims, _signing_key(), algorithm=ALGORITHM)
    return token, datetime.fromtimestamp(expires, tz=UTC)


def verify(token: str) -> AppPrincipal:
    """Check a token's signature, algorithm, issuer, audience and expiry, and read its
    claims. Raises AppTokenError for anything else."""
    try:
        claims = jwt.decode(
            token,
            _signing_key(),
            algorithms=[ALGORITHM],  # pinned: never the token's own say
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        msg = f"not a valid BenchNotes token: {type(exc).__name__}"
        raise AppTokenError(msg) from None
    try:
        user_id = UUID(str(claims["sub"]))
        sysadmin = claims.get("sysadmin")
        if not isinstance(sysadmin, bool):
            msg = "sysadmin must be a boolean"
            raise AppTokenError(msg)
        raw_assignments = claims.get("assignments")
        if not isinstance(raw_assignments, list):
            msg = "assignments must be a list"
            raise AppTokenError(msg)
        assignments = tuple(Context.from_claim(item) for item in raw_assignments)
        raw_ctx = claims.get("ctx")
        ctx = Context.from_claim(raw_ctx) if raw_ctx is not None else None
        name = claims.get("name") or ""
        expires = int(claims["exp"])
        # A token without it (minted before it existed) is its own session: never longer than itself.
        sess_exp = claims.get("sess_exp", expires)
        if isinstance(sess_exp, bool) or not isinstance(sess_exp, int):
            msg = "sess_exp must be an integer"
            raise AppTokenError(msg)
        return AppPrincipal(
            user_id=user_id,
            sysadmin=sysadmin,
            assignments=assignments,
            ctx=ctx,
            issued_at=datetime.fromtimestamp(int(claims["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(expires, tz=UTC),
            session_expires_at=datetime.fromtimestamp(sess_exp, tz=UTC),
            name=name if isinstance(name, str) else "",
        )
    except (ValueError, TypeError, KeyError) as exc:
        msg = f"malformed BenchNotes token claims: {type(exc).__name__}"
        raise AppTokenError(msg) from None
