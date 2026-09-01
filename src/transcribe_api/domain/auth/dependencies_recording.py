"""FastAPI authentication and RBAC dependencies for batch-audio-transcription.

Authentication and role enforcement are provided by the hmcts-fastapi-azure-auth
library. This module wires the library's factories to the application's User DB
model, exposing the same get_current_user / get_allowlisted_user interface that
all route files import.
"""

from __future__ import annotations

import logging

from hmcts_azure_auth import build_current_user_dep
from hmcts_azure_auth import get_allowlisted_user as _lib_get_allowlisted_user
from hmcts_azure_auth.audit import AuditWriter
from hmcts_azure_auth.roles import get_valid_roles
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from transcribe_api.runtime.db_recording import get_engine
from transcribe_api.domain.models_recording import User
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser

logger = logging.getLogger(__name__)


# Highest privilege is listed first; the first match wins.
_ROLE_PRIORITY = ("SystemAdministrator", "LegalTextManager", "Judge", "Normal")


def _resolve_user(azure_user_id: str, email: str, roles: list[str]) -> AuthenticatedUser:
    """Fetch or create the application User from the database.

    Called by the library after identity and roles have been verified.
    Opens its own session so it is independent of request-scoped sessions.
    """
    valid_roles = get_valid_roles()
    roles_set = set(roles)
    primary_role = next(
        (valid_roles[key] for key in _ROLE_PRIORITY if valid_roles.get(key) in roles_set),
        None,
    )

    with Session(get_engine()) as session:
        statement = select(User).where(User.azure_user_id == azure_user_id)
        user = session.exec(statement).first()

        if not user:
            try:
                user = User(
                    email=email,
                    azure_user_id=azure_user_id,
                    role=primary_role or valid_roles.get("Normal"),
                )
                session.add(user)
                session.commit()
                session.refresh(user)
                logger.info("Created new user for azure_user_id=%s", azure_user_id)
            except IntegrityError:
                # Two concurrent first-time requests raced; the other request
                # won the insert. Roll back and load the row it created.
                session.rollback()
                user = session.exec(statement).one()
                logger.info(
                    "Concurrent user creation for azure_user_id=%s; loaded existing row",
                    azure_user_id,
                )
        else:
            if primary_role and user.role != primary_role:
                logger.info(
                    "Syncing User.role from Azure AD for azure_user_id=%s: %s -> %s",
                    azure_user_id,
                    user.role,
                    primary_role,
                )
                user.role = primary_role
                session.add(user)
                session.commit()
                session.refresh(user)
            else:
                logger.info("Found existing user: %s", email)

    return AuthenticatedUser(db_user=user, app_roles=roles)


# FastAPI dependency that resolves the current authenticated User.
# Handles Easy Auth header parsing, JWT verification, identity cross-check,
# and DB lookup/create — all via the library and the resolver above.
get_current_user = build_current_user_dep(_resolve_user)


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
