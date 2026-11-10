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

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlmodel import Session

from transcribe_api.domain.jurisdictions import JurisdictionFeatures, JurisdictionSettings, Membership
from transcribe_api.domain.models_dictation import (
    AuditLog,
    DocumentContent,
    Jurisdiction,
    JurisdictionMembership,
    MinuteVersion,
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
    JurisdictionMembership.__table__,
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


def iac_membership(role: str = "judge") -> Membership:
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
    engine, code: str, *, enabled=True, auto_enrol=False, settings=None, stage="live"
) -> Jurisdiction:
    with Session(engine) as session:
        row = Jurisdiction(
            code=code, name=f"{code} name", enabled=enabled, auto_enrol=auto_enrol, settings=settings or {}, stage=stage
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
            auto_enrol=m.IAC_AUTO_ENROL,
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
    """A first sign-in through the real auth resolver, which creates the user and
    enrols them (needs jurisdiction_db, which points the resolver at ``engine``)."""
    from transcribe_api.domain.auth.dependencies_dictation import _resolve_user

    user = _resolve_user(str(uuid4()), email, ["Judge"]).db_user
    user.__dict__["app_roles"] = ["Judge"]
    return user


def add_member(
    engine, user: User | None, jurisdiction: Jurisdiction, role="judge", email=None, *, tester=False
) -> None:
    with Session(engine) as session:
        session.add(
            JurisdictionMembership(
                jurisdiction_id=jurisdiction.id, user_id=user.id if user else None, email=email, role=role, tester=tester
            )
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


def client_as(app, engine, user: User):
    """A client signed in as ``user``, read afresh on each request as the auth resolver
    does (so a remembered choice is seen). The sign-in is the app's: a second
    client_as on the same app signs every client in as the second user."""
    from fastapi.testclient import TestClient

    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    roles = user.__dict__.get("app_roles", ["Judge"])

    def current_user():
        with Session(engine) as session:
            fresh = session.get(User, user.id)
        fresh.__dict__["app_roles"] = roles
        return fresh

    app.dependency_overrides[get_current_user] = current_user
    return TestClient(app)
