"""FastAPI authentication and RBAC dependencies for batch-audio-transcription.

Authentication is the BenchNotes access token (domain/auth/token_auth.py); role
enforcement is the hmcts-fastapi-azure-auth library's, over the roles derived from the
token's active context (domain/auth/legacy_roles.py), until the central permission checks
replace them (DIAAT-376). This module wires the factories to the application's User DB
model, exposing the same get_current_user / get_allowlisted_user interface that all
route files import.
"""

from __future__ import annotations

import logging

from hmcts_azure_auth import get_allowlisted_user as _lib_get_allowlisted_user
from hmcts_azure_auth.audit import AuditWriter
from sqlmodel import Session

from transcribe_api.domain.auth.app_token import AppPrincipal
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.legacy_roles import legacy_app_roles
from transcribe_api.domain.auth.token_auth import build_token_user_dep
from transcribe_api.domain.auth.user_loading import load_user
from transcribe_api.domain.jurisdictions import jurisdiction_dependency
from transcribe_api.runtime.db_recording import get_engine

logger = logging.getLogger(__name__)


def _resolve_user(principal: AppPrincipal) -> AuthenticatedUser:
    """The application User the BenchNotes token names, with what it says about them.

    Opens its own session so it is independent of request-scoped sessions. A user is
    created at sign-in (domain/auth/sessions.py), never here.
    """
    with Session(get_engine()) as session:
        user = load_user(session, principal)
    return AuthenticatedUser(
        db_user=user,
        app_roles=legacy_app_roles(principal.ctx, principal.sysadmin),
        ctx=principal.ctx,
        sysadmin=principal.sysadmin,
        assignments=principal.assignments,
    )


# FastAPI dependency that resolves the current authenticated User from a verified
# BenchNotes access token only, acting in a chosen context (403 context_required without
# one) — see domain/auth/token_auth.py.
get_current_user = build_token_user_dep(_resolve_user)

# The request's jurisdiction (DIAAT-322), bound to this area's user.
get_active_jurisdiction = jurisdiction_dependency(get_current_user)


def get_allowlisted_user(
    required_roles_all: list[str] | None = None,
    required_roles_any: list[str] | None = None,
    audit_writer: AuditWriter | None = None,
):
    """Factory returning a FastAPI dependency that enforces role requirements.

    Thin wrapper around the library's get_allowlisted_user, binding it to this
    application's get_current_user so route files need no import changes.

    With no role arguments, any authenticated user is permitted.
    required_roles_all: user must hold every listed role.
    required_roles_any: user must hold at least one of the listed roles.
    audit_writer: optional callback fired with an AuditEvent on every 403.
    """
    return _lib_get_allowlisted_user(
        required_roles_all=required_roles_all,
        required_roles_any=required_roles_any,
        audit_writer=audit_writer,
        current_user_dep=get_current_user,
    )
