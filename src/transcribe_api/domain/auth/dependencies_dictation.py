"""FastAPI authentication and RBAC dependencies for courtstranscribe.

Authentication is the BenchNotes access token (domain/auth/token_auth.py); role
enforcement is the hmcts-fastapi-azure-auth library's, over the roles derived from the
token's active context (domain/auth/legacy_roles.py), until the central permission checks
replace them (DIAAT-376). This module wires the factories to the application's User DB
model, exposing the same get_current_user / get_allowlisted_user interface that all
route files already import.
"""

from __future__ import annotations

import logging
import re

from hmcts_azure_auth import get_allowlisted_user as _lib_get_allowlisted_user
from hmcts_azure_auth.audit import AuditWriter
from sqlmodel import Session

from transcribe_api.domain.auth.app_token import AppPrincipal
from transcribe_api.domain.auth.auth_models_dictation import AuthenticatedUser
from transcribe_api.domain.auth.legacy_roles import legacy_app_roles
from transcribe_api.domain.auth.token_auth import build_token_user_dep
from transcribe_api.domain.auth.user_loading import load_user
from transcribe_api.domain.jurisdictions import jurisdiction_dependency
from transcribe_api.runtime.db_connection import get_engine

logger = logging.getLogger(__name__)

# The hmcts_azure_auth library logs the user's email address as part of its
# authentication-validated and unauthorised-access-attempt audit trail.
# We cannot change the library's log calls, so we apply a logging.Filter that
# redacts any email-shaped token before the record is emitted to any handler
# (Application Insights, console, etc.).  The filter mutates record.msg and
# record.args in place; downstream handlers (including pytest caplog) therefore
# never see the raw email.
# Local part follows RFC 5322 dot-atom, which permits far more than alphanumerics:
# ! # $ % & ' * + / = ? ^ _ ` { | } ~ - and '.'.  A narrower class (e.g. only
# [a-zA-Z0-9._%+-]) leaves addresses such as o'brien@... or foo!bar@... unredacted,
# so we match the full permitted set.  Over-matching is acceptable for a redactor.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


class _RedactEmailFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _EMAIL_RE.sub("[redacted]", record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _EMAIL_RE.sub("[redacted]", v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                args = record.args if isinstance(record.args, tuple) else (record.args,)
                record.args = tuple(
                    _EMAIL_RE.sub("[redacted]", a) if isinstance(a, str) else a
                    for a in args
                )
        return True


# Python logging filters do NOT propagate from a parent logger to its children:
# adding a filter to hmcts_azure_auth only covers records emitted directly to that
# logger, not records from hmcts_azure_auth.jwt, hmcts_azure_auth.roles, etc.
# Ensure the known sub-loggers exist, then attach the same filter instance to every
# currently-registered logger in the hmcts_azure_auth.* namespace.
_redact_filter = _RedactEmailFilter()
_marker_attr = "_courtstranscribe_email_redaction_filter_attached"
for _name in (
    "hmcts_azure_auth",
    "hmcts_azure_auth.dependencies",
    "hmcts_azure_auth.jwt",
    "hmcts_azure_auth.roles",
):
    logging.getLogger(_name)
for _logger_name in list(logging.root.manager.loggerDict):
    if _logger_name == "hmcts_azure_auth" or _logger_name.startswith("hmcts_azure_auth."):
        _logger = logging.getLogger(_logger_name)
        if getattr(_logger, _marker_attr, False):
            continue
        _logger.addFilter(_redact_filter)
        setattr(_logger, _marker_attr, True)


def _resolve_user(principal: AppPrincipal) -> AuthenticatedUser:
    """The application User the BenchNotes token names, with what it says about them.

    Opens its own session so it is independent of request-scoped sessions. A user is
    created at sign-in (domain/auth/sessions.py), never here: a token for a user who is
    not in the database is refused, so a deleted or deactivated user is not brought back
    by a request.
    """
    with Session(get_engine()) as session:
        user = load_user(session, principal)
    logger.info("Found existing user id=%s", user.id)
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

# The same for the no-context endpoints (who am I, choose a context, leave a jurisdiction,
# the public jurisdictions): any signed-in user, with or without a context.
get_signed_in_user = build_token_user_dep(_resolve_user, require_context=False)

# The request's jurisdiction (DIAAT-322): domain/jurisdictions.py.
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
