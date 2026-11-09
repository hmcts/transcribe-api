"""FastAPI authentication and permission dependencies for the hearing-recording routes.

Authentication is the BenchNotes access token (domain/auth/token_auth.py); what the caller
may do is the central permission model's (domain/auth/authorisation.py, DIAAT-376). This
module wires them to the application's User DB model: ``get_current_user`` (a user acting in
a context) and ``require_permission``, which every recording route declares.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from transcribe_api.domain.auth.app_token import AppPrincipal
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.authorisation import build_require_permission
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


# The permission every recording route declares (domain/auth/authorisation.py).
require_permission = build_require_permission(get_current_user)
