"""Who may call a route (DIAAT-376): ``require_permission``, deny by default.

Every route of the API declares exactly one of:

* ``require_permission(*permissions)``: the caller must hold at least one of them. What a
  caller holds is derived, with no database read, from the verified BenchNotes token: the
  role of the context they act in plus, for a SystemAdministrator, the system-level
  permissions (``permissions.permissions_for``). The dependency returns the authenticated
  user, as ``get_current_user`` does, so a route takes ``current_user = Depends(require_
  permission("dictation.use"))`` and needs nothing else;
* an explicit exemption (``public_endpoint``, ``sign_in_endpoint``, ``signed_in_endpoint``,
  ``signed_url_endpoint``), for the few routes that need no permission. They are named in
  tests/unit/test_permission_matrix.py, and a route with neither fails that test.

Refusals (``{"detail": {"code": ..., "message": ...}}``, 403):

``context_required``
    no (jurisdiction, role) context is chosen and what the caller holds without one (the
    SystemAdministrator's permissions) does not cover the route.
``permission_denied``
    the context's role does not grant any of the permissions. Audited: an ``ACCESS_DENIED`` row
    (outcome ``denied``, the active context stamped on it) with the caller, the role held and the
    permissions asked for.
``dictation_disabled``
    the caller holds ``dictation.use`` but dictation is switched off (domain/feature_flags.py,
    DIAAT-388). Audited like ``permission_denied``.
``jurisdiction_not_in_scope``
    ``assert_jurisdiction_in_scope``: a jurisdiction-scoped permission (users.manage,
    jurisdictions.manage, ...) was used on a jurisdiction other than the one the caller acts in
    (a JurisdictionSuperAdministrator's apply to every jurisdiction).

``require_permission`` is built per area (``build_require_permission``): the dictation and the
recording routes each have their own authenticated-user type. The callable for a set of
permissions is created once, so FastAPI resolves it once per request even when a route and
its sub-dependencies both name it.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any

from fastapi import Depends, Request
from hmcts_azure_auth.audit import AuditEvent, AuditEventType
from starlette.concurrency import run_in_threadpool

from transcribe_api.domain import feature_flags
from transcribe_api.domain.audit import ACCESS_DENIED, DENIED, record_audit, write_audit_event
from transcribe_api.domain.auth import context as auth_context
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.context import context_required, error
from transcribe_api.domain.auth.permissions import (
    GLOBAL_ROLES,
    PERMISSIONS,
    ROLE_SYSTEM_ADMIN,
    permissions_for,
)
from transcribe_api.runtime.log_sanitization import sanitize_for_log

PERMISSION_DENIED = "permission_denied"
DICTATION_DISABLED = "dictation_disabled"
JURISDICTION_NOT_IN_SCOPE = "jurisdiction_not_in_scope"

DICTATION_USE = "dictation.use"

# Attributes the generated permission test reads from a route's dependency tree.
PERMISSIONS_ATTR = "__authz_permissions__"
EXEMPT_ATTR = "__authz_exempt__"

EXEMPT_PUBLIC = "public"  # no authentication at all: health and version
EXEMPT_SIGN_IN = "sign_in"  # authenticates itself: the Entra token (or a local dev persona)
EXEMPT_SIGNED_IN = "signed_in"  # any signed-in user, with or without a context (contract s3)
EXEMPT_SIGNED_URL = "signed_url"  # authenticated by a signed, expiring URL


def declared_permissions(call: object) -> frozenset[str] | None:
    """The permissions a ``require_permission`` dependency asks for, or None if ``call`` is not one."""
    value = getattr(call, PERMISSIONS_ATTR, None)
    return value if isinstance(value, frozenset) else None


def declared_exemption(call: object) -> str | None:
    """The exemption an exemption marker stands for, or None if ``call`` is not one."""
    value = getattr(call, EXEMPT_ATTR, None)
    return value if isinstance(value, str) else None


def mark_exempt(call: Callable, kind: str) -> Callable:
    """Declare that the route using ``call`` as a dependency needs no permission, and why."""
    setattr(call, EXEMPT_ATTR, kind)
    return call


async def public_endpoint() -> None:
    """Dependency of a route that needs no authentication (health and version)."""


async def sign_in_endpoint() -> None:
    """Dependency of ``POST /api/auth/session``, which checks the Entra ID token (or a local dev
    persona) itself and returns the BenchNotes token every other route then needs."""


async def signed_url_endpoint() -> None:
    """Dependency of a route whose URL is signed and expires (local audio, for Azure Speech)."""


mark_exempt(public_endpoint, EXEMPT_PUBLIC)
mark_exempt(sign_in_endpoint, EXEMPT_SIGN_IN)
mark_exempt(signed_url_endpoint, EXEMPT_SIGNED_URL)


# ---------------------------------------------------------------------------
# What a user holds
# ---------------------------------------------------------------------------


def permissions_of(user: object) -> list[str]:
    """The permissions of the user's token: those of the role they act in, plus the system ones
    for a SystemAdministrator. With no context only the latter (none for anyone else)."""
    ctx = auth_context.context_of(user)
    return permissions_for(ctx.role if ctx else None, auth_context.sysadmin_of(user))


def has_permission(user: object, permission: str) -> bool:
    return permission in permissions_of(user)


def _held_roles(ctx: Context | None, sysadmin: bool) -> list[str]:
    roles = [ctx.role] if ctx else []
    if sysadmin:
        roles.append(ROLE_SYSTEM_ADMIN)
    return roles


# ---------------------------------------------------------------------------
# require_permission
# ---------------------------------------------------------------------------


def _check_known(permissions: tuple[str, ...]) -> None:
    if not permissions:
        msg = "require_permission needs at least one permission"
        raise ValueError(msg)
    unknown = sorted(set(permissions) - set(PERMISSIONS))
    if unknown:
        msg = f"unknown permission: {', '.join(unknown)}"
        raise ValueError(msg)


async def _audit_denial(
    request: Request, user: object, wanted: tuple[str, ...], reason: str
) -> None:
    """The ACCESS_DENIED row of a refusal, with the active context (domain/audit.py). Off the
    event loop, awaited; never raises (fail-open, as every audit write)."""
    ctx = auth_context.context_of(user)
    event = AuditEvent(
        event_type=AuditEventType.ACCESS_DENIED,
        user_id=str(getattr(user, "id", "unknown")),
        email=str(getattr(user, "email", "") or ""),
        held_roles=_held_roles(ctx, auth_context.sysadmin_of(user)),
        required_roles=list(wanted),  # the permissions asked for (the column's name is historical)
        resource=sanitize_for_log(f"{request.method} {request.url.path}"),
        detail={"reason": reason},
        client_ip=request.client.host if request.client else None,
    )
    await asyncio.to_thread(write_audit_event, event)


async def enforce_permission(request: Request, user: object, wanted: tuple[str, ...]) -> None:
    """Raise the 403 for a caller who may not use a route that needs one of ``wanted``."""
    ctx = auth_context.context_of(user)
    granted = set(permissions_of(user))
    held = granted & set(wanted)
    if not held:
        if ctx is None:
            raise context_required()
        await _audit_denial(request, user, wanted, PERMISSION_DENIED)
        raise error(403, PERMISSION_DENIED, "Your role does not allow this.")
    # Dictation off (DIAAT-388): holding dictation.use is not enough. A route that also accepts
    # another permission the caller holds is not a dictation route for them.
    if held == {DICTATION_USE} and not await run_in_threadpool(feature_flags.is_dictation_enabled):
        await _audit_denial(request, user, wanted, DICTATION_DISABLED)
        raise error(403, DICTATION_DISABLED, "Dictation is switched off.")


def build_require_permission(current_user_dep: Callable) -> Callable[..., Callable]:
    """``require_permission`` for one area: the dependency it returns resolves the caller with
    ``current_user_dep`` (which also refuses a request with no context from anyone but a
    SystemAdministrator, and a stale tab's X-Jurisdiction / X-Role), then checks the permission."""

    @functools.cache
    def build(permissions: tuple[str, ...]) -> Callable:
        async def require(request: Request, current_user: Any = Depends(current_user_dep)) -> Any:
            await enforce_permission(request, current_user, permissions)
            return current_user

        require.__name__ = require.__qualname__ = "require_permission[" + ",".join(permissions) + "]"
        setattr(require, PERMISSIONS_ATTR, frozenset(permissions))
        return require

    def require_permission(*permissions: str) -> Callable:
        """The caller must hold at least one of ``permissions`` (see the module docstring)."""
        _check_known(permissions)
        return build(tuple(sorted(set(permissions))))

    return require_permission


# ---------------------------------------------------------------------------
# Jurisdiction scope
# ---------------------------------------------------------------------------


def jurisdiction_scope(user: object) -> frozenset[str] | None:
    """The jurisdiction codes a jurisdiction-scoped permission of the user applies to: None for
    every jurisdiction (a global role), else just the one they act in (none without a context)."""
    ctx = auth_context.context_of(user)
    if ctx is None:
        return frozenset()
    if ctx.role in GLOBAL_ROLES:
        return None
    return frozenset({ctx.jurisdiction.upper()}) if ctx.jurisdiction else frozenset()


def jurisdiction_in_scope(user: object, code: str) -> bool:
    scope = jurisdiction_scope(user)
    return scope is None or code.strip().upper() in scope


def assert_jurisdiction_in_scope(user: object, code: str) -> None:
    """403 ``jurisdiction_not_in_scope`` unless the jurisdiction-scoped permission the route
    required applies to ``code``: the jurisdiction the caller acts in, or any for a
    JurisdictionSuperAdministrator. Audited. For the routes that name the jurisdiction they work
    on (``/admin/jurisdictions/{code}``); a route that works on the active jurisdiction is in
    scope by construction."""
    if jurisdiction_in_scope(user, code):
        return
    record_audit(
        ACCESS_DENIED,
        actor_id=getattr(user, "id", "unknown"),
        outcome=DENIED,
        object_type="jurisdiction",
        detail={"reason": JURISDICTION_NOT_IN_SCOPE, "code": sanitize_for_log(code.strip().upper())},
    )
    raise error(403, JURISDICTION_NOT_IN_SCOPE, "That jurisdiction is not the one you are working in.")


def ensure_permission(user: object, permission: str) -> None:
    """For a route that needs a second permission only on some paths (creating a jurisdiction
    needs ``jurisdiction.create`` on top of ``jurisdictions.manage``): 403 ``permission_denied``
    unless the user holds it. Audited."""
    if has_permission(user, permission):
        return
    record_audit(
        ACCESS_DENIED,
        actor_id=getattr(user, "id", "unknown"),
        outcome=DENIED,
        object_type="permission",
        detail={"reason": PERMISSION_DENIED, "permission": permission},
    )
    raise error(403, PERMISSION_DENIED, "Your role does not allow this.")
