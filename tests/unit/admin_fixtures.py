"""A signed-in world for the administration tests (DIAAT-383, 391, 392, 394, 395, 396, 398).

Real authentication end to end, as tests/unit/test_session_api.py does it (no dependency
overrides): people sign in with an Entra ID token the real verifier accepts, hold role assignments
in an in-memory database, and call the real routes with the BenchNotes token the API minted for
the role they act as. ``World.person`` makes someone who has signed in, ``Person.headers`` signs
them in again as a role (reading their assignments afresh, as the web app's re-mint does).

``jurisdiction_db`` (tests/unit/conftest.py) is the database: SQLite in memory with the jurisdiction
tables, so a test that depends on PostgreSQL's constraints or locks uses ``fresh_postgres`` instead.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.unit import auth_fixtures as af
from tests.unit.jurisdiction_fixtures import add_iac_as_migrated, add_jurisdiction
from transcribe_api.domain.assignments import add_assignment
from transcribe_api.domain.models_dictation import AuditLog, Jurisdiction, RoleAssignment, User

JUDGE = "Judge"
LTM = "LegalTextManager"
ADMIN = "JurisdictionAdministrator"
SUPER = "JurisdictionSuperAdministrator"
SYSTEM = "SystemAdministrator"


def build_app() -> FastAPI:
    """The routes this phase added, with those they work beside, as the API mounts them."""
    import transcribe_api.api.routes_dictation
    from transcribe_api.api import (
        routes_access_requests,
        routes_admin_flags,
        routes_admin_users,
        routes_dictation_privileged,
        routes_flags,
        routes_jurisdictions,
        routes_recording,
        routes_session,
    )

    app = FastAPI()
    app.state.limiter = routes_recording.limiter
    app.include_router(routes_recording.router)  # declares its own prefix, /api/v1
    for router in (
        routes_flags.router,
        transcribe_api.api.routes_dictation.router,
        routes_jurisdictions.router,
        routes_session.router,
        routes_dictation_privileged.router,
        routes_admin_users.router,
        routes_admin_flags.router,
        routes_access_requests.router,
        routes_access_requests.admin_router,
    ):
        app.include_router(router, prefix="/api")
    return app


class Person:
    """Someone with a fixed Entra identity, so they can sign in again as another role."""

    def __init__(self, world: World, name: str, *, email: str | None = None, sysadmin: bool = False) -> None:
        self.world = world
        self.oid = str(uuid4())
        self.name = name
        self.email = email or f"{name.lower().replace(' ', '.')}.{uuid4().hex[:6]}@justice.gov.uk"
        self.sysadmin = sysadmin
        self.user_id: UUID | None = None

    def _entra(self) -> str:
        return af.entra_token(
            oid=self.oid,
            preferred_username=self.email,
            name=self.name,
            roles=[SYSTEM] if self.sysadmin else [],
        )

    def sign_in(self, context: tuple[str | None, str] | None = None):
        headers = {"Authorization": f"Bearer {self._entra()}"}
        if context is not None:
            headers["X-Requested-Context"] = f"{context[0] or '-'}:{context[1]}"
        response = self.world.client.post("/api/auth/session", headers=headers)
        if response.status_code == 200 and self.user_id is None:
            self.user_id = UUID(response.json()["me"]["user"]["id"])
        return response

    def headers(self, role: str | None = None, jurisdiction: str | None = None) -> dict[str, str]:
        """A bearer for acting as ``role`` in ``jurisdiction`` (None for the global role), or with
        no context when ``role`` is None. They must hold it: the token has what they hold now."""
        response = self.sign_in((jurisdiction, role) if role else None)
        assert response.status_code == 200, response.text
        if role:
            assert response.json()["me"]["active"] is not None, f"{self.name} does not hold {role} in {jurisdiction}"
        return {"Authorization": f"Bearer {response.json()['token']}"}

    def call(self, method: str, path: str, *, role=None, jurisdiction=None, **kwargs):
        return self.world.client.request(method, path, headers=self.headers(role, jurisdiction), **kwargs)

    def get(self, path: str, **kwargs):
        return self.call("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.call("POST", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self.call("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.call("DELETE", path, **kwargs)

    def patch(self, path: str, **kwargs):
        return self.call("PATCH", path, **kwargs)


class World:
    def __init__(self, engine, client: TestClient) -> None:
        self.engine = engine
        self.client = client
        self.iac = add_iac_as_migrated(engine)
        self.et = add_jurisdiction(engine, "ET")
        self.jurisdictions: dict[str, Jurisdiction] = {"IAC": self.iac, "ET": self.et}

    def jurisdiction(self, code: str) -> Jurisdiction:
        if code not in self.jurisdictions:
            self.jurisdictions[code] = add_jurisdiction(self.engine, code)
        return self.jurisdictions[code]

    def person(self, name: str, *held: tuple[str | None, str], sysadmin: bool = False, email: str | None = None) -> Person:
        """Someone who has signed in (so they exist) and holds ``held``: (jurisdiction code or None, role)."""
        person = Person(self, name, email=email, sysadmin=sysadmin)
        response = person.sign_in()
        assert response.status_code == 200, response.text
        for code, role in held:
            self.give(person, code, role)
        return person

    def give(self, person: Person, code: str | None, role: str) -> None:
        with Session(self.engine) as session:
            add_assignment(
                session,
                role=role,
                jurisdiction_id=self.jurisdiction(code).id if code else None,
                user_id=person.user_id,
            )
            session.commit()

    def assignments_of(self, person: Person) -> set[tuple[str | None, str]]:
        with Session(self.engine) as session:
            rows = session.exec(
                select(RoleAssignment, Jurisdiction)
                .join(Jurisdiction, Jurisdiction.id == RoleAssignment.jurisdiction_id, isouter=True)
                .where(RoleAssignment.user_id == person.user_id)
            ).all()
        return {(j.code if j else None, ra.role) for ra, j in rows}

    def pending_for(self, email: str) -> set[tuple[str | None, str]]:
        with Session(self.engine) as session:
            rows = session.exec(
                select(RoleAssignment, Jurisdiction)
                .join(Jurisdiction, Jurisdiction.id == RoleAssignment.jurisdiction_id, isouter=True)
                .where(RoleAssignment.pending_email == email.lower())
            ).all()
        return {(j.code if j else None, ra.role) for ra, j in rows}

    def audit(self, event_type: str) -> list[AuditLog]:
        with Session(self.engine) as session:
            return list(
                session.exec(select(AuditLog).where(AuditLog.event_type == event_type).order_by(AuditLog.timestamp)).all()
            )

    def user(self, person: Person) -> User:
        with Session(self.engine) as session:
            return session.get(User, person.user_id)


def make_world(engine, monkeypatch) -> World:
    """The app on ``engine`` (the in-memory jurisdiction database), with Entra stubbed."""
    from transcribe_api.api import routes_dictation_privileged, routes_jurisdictions, routes_session
    from transcribe_api.domain import audit, feature_flags
    from transcribe_api.domain.auth import dependencies_recording
    from transcribe_api.runtime import db_recording

    af.stub_entra(monkeypatch)
    monkeypatch.setattr(routes_session, "get_engine", lambda: engine)
    monkeypatch.setattr(routes_dictation_privileged, "get_engine", lambda: engine)
    monkeypatch.setattr(routes_jurisdictions, "get_engine", lambda: engine)
    monkeypatch.setattr(feature_flags, "get_engine", lambda: engine)
    # Audit rows written from the event loop are handed to a worker thread in the API; the in-memory SQLite
    # database here has one shared connection, so they are written where they are made.
    monkeypatch.setattr(audit, "_submit", audit._persist)
    # The recording area's own engine, for its auth dependency and its session.
    monkeypatch.setattr(dependencies_recording, "get_engine", lambda: engine)
    monkeypatch.setattr(db_recording, "get_engine", lambda: engine)
    return World(engine, TestClient(build_app(), raise_server_exceptions=False))
