"""Jurisdictions for tests (DIAAT-322).

``IAC_JURISDICTION_ID`` and ``iac_membership``: the IAC as migration 0004 creates it,
for tests that do not look at jurisdictions (tests/unit/conftest.py makes every user
an IAC judge). The rest is the SQLite harness for tests of jurisdictions themselves:
the ``jurisdiction_db`` fixture (tests/unit/conftest.py) and helpers on its engine.
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlmodel import Session

from transcribe_api.domain.assignments import add_assignment
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.jurisdictions import JurisdictionFeatures, JurisdictionSettings, Membership
from transcribe_api.domain.models_dictation import (
    AccessRequest,
    AccessRequestRole,
    AuditLog,
    DocumentContent,
    Jurisdiction,
    JurisdictionTemplate,
    MinuteVersion,
    RoleAssignment,
    Tag,
    Transcription,
    TranscriptionJob,
    TranscriptionTagAssociation,
    UploadJurisdiction,
    User,
)
from transcribe_api.domain.models_recording import Caller, SpeechBatchJob

IAC_JURISDICTION_ID = UUID("6a1c0f3e-3b7d-4c2a-9e51-1ac000000001")

# Every table a jurisdiction test touches, created on SQLite by jurisdiction_db.
JURISDICTION_TABLES = [
    User.__table__,
    Jurisdiction.__table__,
    JurisdictionTemplate.__table__,
    RoleAssignment.__table__,
    AccessRequest.__table__,
    AccessRequestRole.__table__,
    UploadJurisdiction.__table__,
    Transcription.__table__,
    TranscriptionJob.__table__,
    MinuteVersion.__table__,
    Tag.__table__,
    TranscriptionTagAssociation.__table__,
    DocumentContent.__table__,
    AuditLog.__table__,
    Caller.__table__,
    SpeechBatchJob.__table__,
]


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):
    return "JSON"


def iac_membership(role: str = "Judge") -> Membership:
    return Membership(
        jurisdiction_id=IAC_JURISDICTION_ID,
        code="IAC",
        name="Immigration and Asylum Chamber",
        role=role,
        settings=JurisdictionSettings(template="iac_decision", features=JurisdictionFeatures(hearing_recordings=True)),
    )


@functools.cache
def load_migration(file_name: str) -> ModuleType:
    """An Alembic revision module, by file name, for the constants it declares."""
    from transcribe_api.runtime import migrations

    path = Path(migrations.alembic_config().get_main_option("script_location")) / "versions" / file_name
    spec = importlib.util.spec_from_file_location(file_name.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_jurisdiction(
    engine, code: str, *, enabled=True, settings=None, stage="live"
) -> Jurisdiction:
    with Session(engine) as session:
        row = Jurisdiction(
            code=code, name=f"{code} name", enabled=enabled, settings=settings or {}, stage=stage
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def add_iac_as_migrated(engine) -> Jurisdiction:
    """The IAC row exactly as migration 0004 inserts it."""
    m = load_migration("0004_jurisdictions.py")
    with Session(engine) as session:
        row = Jurisdiction(
            id=UUID(m.IAC_ID),
            code=m.IAC_CODE,
            name=m.IAC_NAME,
            enabled=m.IAC_ENABLED,
            settings=m.IAC_SETTINGS,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def add_user(engine, email: str | None = None, roles: tuple[str, ...] = ("Judge",)) -> User:
    with Session(engine) as session:
        user = User(email=email or f"{uuid4()}@example.test", azure_user_id=str(uuid4()))
        session.add(user)
        session.commit()
        session.refresh(user)
    user.__dict__["app_roles"] = list(roles)
    return user


def sign_up(engine, email: str) -> User:
    """A first sign-in through the real sign-in (domain/auth/sessions.sign_in), which creates the
    user and gives them what was assigned to their email beforehand, and nothing else (needs
    jurisdiction_db)."""
    from transcribe_api.domain.auth.sessions import sign_in

    with Session(engine) as session:
        user = sign_in(session, azure_user_id=str(uuid4()), email=email, name="")
    user.__dict__["app_roles"] = ["Judge"]
    return user


_ASSIGNED = {"judge": "Judge", "admin": "JurisdictionAdministrator"}


def add_member(
    engine, user: User | None, jurisdiction: Jurisdiction, role="judge", email=None, *, tester=False
) -> None:
    """Give ``user`` (or, with none, the pending ``email``) a role in a jurisdiction: ``judge`` or
    ``admin`` as the member roles of jurisdictions_admin, or a role's own name."""
    with Session(engine) as session:
        add_assignment(
            session,
            role=_ASSIGNED.get(role, role),
            jurisdiction_id=jurisdiction.id,
            user_id=user.id if user else None,
            email=None if user else email,
            tester=tester,
        )
        session.commit()


def jurisdiction_route_app():
    """The dictation and jurisdiction routes, as the API mounts them."""
    from fastapi import FastAPI

    import transcribe_api.api.routes_dictation
    from transcribe_api.api.routes_jurisdictions import router

    app = FastAPI()
    app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    app.include_router(router, prefix="/api")
    return app


AUTO = object()


def acting_as(engine, user: User, ctx=AUTO, sysadmin: bool | None = None, *, area: str = "dictation"):
    """A ``get_current_user`` replacement for ``user`` as the BenchNotes token the web app would hold
    for them (DIAAT-373), read afresh on each request as the auth resolver does. ``area``: whose
    AuthenticatedUser it returns, "dictation" or "recording".

    ``ctx``: the context they act in, a Context or a (jurisdiction code, role) pair, or None for
    none chosen. Left out, it is the one a tab would have: the assignment in the jurisdiction the
    request names with ``X-Jurisdiction`` if they hold one there, else their first given assignment (so
    a user with one pair just acts in it). And as for a real token, a header that names another
    jurisdiction is 409 ``context_changed``, and no context is 403 ``context_required``. A SystemAdministrator
    also holds the SystemAdministrator context, after their assignments. ``sysadmin``: the Entra role; left out, whether ``SystemAdministrator``
    is among the roles ``add_user`` was given."""
    import importlib

    from transcribe_api.domain.assignments import distinct_contexts, load_assignments
    from transcribe_api.domain.auth.context import check_advisory_headers, context_required

    authenticated_user = importlib.import_module(f"transcribe_api.domain.auth.auth_models_{area}").AuthenticatedUser
    fake_roles = user.__dict__.get("app_roles", ["Judge"])
    is_sysadmin = ("SystemAdministrator" in fake_roles) if sysadmin is None else sysadmin

    def current_user(request: Request):
        with Session(engine) as session:
            fresh = session.get(User, user.id)
            assignments = load_assignments(session, user.id)
        system = [Context(None, "SystemAdministrator")] if is_sysadmin else []
        held = [*distinct_contexts(assignments), *system]
        if ctx is AUTO:
            named = (request.headers.get("X-Jurisdiction") or "").strip().upper()
            context = next((c for c in held if c.jurisdiction == named), None) if named else None
            context = context or (held[0] if held else None)
        else:
            context = Context(*ctx) if isinstance(ctx, tuple) else ctx
        if context is None:  # as build_token_user_dep does
            raise context_required()
        check_advisory_headers(request, context)
        return authenticated_user(
            db_user=fresh,
            ctx=context,
            sysadmin=is_sysadmin,
            assignments=tuple(held),
        )

    return current_user


def client_as(app, engine, user: User, ctx=AUTO, sysadmin: bool | None = None):
    """A client of the dictation app signed in as ``user`` (see ``acting_as``). The sign-in is the
    app's: a second client_as on the same app signs every client in as the second user."""
    from fastapi.testclient import TestClient

    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    app.dependency_overrides[get_current_user] = acting_as(engine, user, ctx, sysadmin)
    return TestClient(app)
