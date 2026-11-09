"""Sign-in, the BenchNotes token and the user's context (DIAAT-373, DIAAT-382, DIAAT-386, DIAAT-390).

Real authentication end to end (no dependency overrides): an Entra ID token (or a local dev
persona) is exchanged at POST /api/auth/session for a BenchNotes token, which every other
route then requires. The database is the in-memory SQLite of ``jurisdiction_db``.
"""

from __future__ import annotations

import time
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.unit import auth_fixtures as af
from tests.unit.jurisdiction_fixtures import add_iac_as_migrated, add_jurisdiction, add_member
from transcribe_api.domain.auth import app_token
from transcribe_api.domain.models_dictation import AuditLog, RoleAssignment, User

pytestmark = pytest.mark.real_jurisdictions

SUPER = "JurisdictionSuperAdministrator"
ADMIN = "JurisdictionAdministrator"


@pytest.fixture
def db(jurisdiction_db, monkeypatch):
    from transcribe_api.api import routes_dictation_privileged, routes_session

    af.stub_entra(monkeypatch)
    monkeypatch.setattr(routes_session, "get_engine", lambda: jurisdiction_db)
    monkeypatch.setattr(routes_dictation_privileged, "get_engine", lambda: jurisdiction_db)
    yield jurisdiction_db
    af.unstub_entra()


@pytest.fixture
def world(db):
    """The IAC (as migrated) and the ET."""
    iac = add_iac_as_migrated(db)
    et = add_jurisdiction(db, "ET")
    return {"iac": iac, "et": et}


@pytest.fixture
def client(db):
    import transcribe_api.api.routes_dictation
    from transcribe_api.api import routes_dictation_privileged
    from transcribe_api.api.routes_flags import router as flags_router
    from transcribe_api.api.routes_jurisdictions import router as jurisdictions_router
    from transcribe_api.api.routes_session import router as session_router

    app = FastAPI()
    app.include_router(flags_router, prefix="/api")
    app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    app.include_router(jurisdictions_router, prefix="/api")
    app.include_router(session_router, prefix="/api")
    app.include_router(routes_dictation_privileged.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


class Person:
    """Someone signing in with Entra: a fixed identity, so they can sign in again."""

    def __init__(self, client, name="Judge Real", email=None, roles=()):
        self.client = client
        self.oid = str(uuid4())
        self.name = name
        self.email = email or f"{uuid4().hex[:8]}@justice.gov.uk"
        self.roles = list(roles)

    def entra(self, **overrides) -> str:
        claims = {"oid": self.oid, "preferred_username": self.email, "name": self.name, "roles": self.roles}
        return af.entra_token(**(claims | overrides))

    def sign_in(self, requested_context: str | None = None, **overrides):
        headers = {"Authorization": f"Bearer {self.entra(**overrides)}"}
        if requested_context is not None:
            headers["X-Requested-Context"] = requested_context
        return self.client.post("/api/auth/session", headers=headers)


def bearer(token: str, **headers) -> dict:
    return {"Authorization": f"Bearer {token}", **headers}


def claims_of(token: str) -> dict:
    import jwt

    return jwt.decode(token, af.APP_KEY, algorithms=["HS256"], audience="benchnotes")


def user_with(client, db, *assignments, roles=(), **person_kwargs):
    """A signed-in user holding ``assignments`` ((jurisdiction row or None, role) pairs)."""
    person = Person(client, roles=roles, **person_kwargs)
    first = person.sign_in()
    assert first.status_code == 200, first.text
    user = User(id=UUID(first.json()["me"]["user"]["id"]))
    with Session(db) as session:
        row = session.get(User, user.id)
    for jurisdiction, role in assignments:
        add_member(db, row, jurisdiction, role=role) if jurisdiction else _add_global(db, row, role)
    person.user_id = row.id
    return person


def _add_global(db, user, role):
    from transcribe_api.domain.assignments import add_assignment

    with Session(db) as session:
        add_assignment(session, role=role, jurisdiction_id=None, user_id=user.id)
        session.commit()


# --- a new user -------------------------------------------------------------------------


def test_a_new_user_has_no_role_and_no_access(client, world, db):
    person = Person(client)
    response = person.sign_in()

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"token", "expires_at", "me"}
    me = body["me"]
    assert me["status"] == "no_access"
    assert me["assignments"] == [] and me["active"] is None and me["permissions"] == []
    assert me["pending_requests"] == []
    assert me["user"]["email"] == person.email and me["user"]["name"] == "Judge Real"
    # Created with no role: nothing of the old default role.
    with Session(db) as session:
        user = session.exec(select(User)).one()
        assert user.role is None and user.is_active is True and user.name == "Judge Real"
        assert session.exec(select(RoleAssignment)).all() == []
    claims = claims_of(body["token"])
    assert claims["ctx"] is None and claims["assignments"] == [] and claims["sysadmin"] is False
    assert claims["sub"] == me["user"]["id"]


def test_a_new_user_is_not_enrolled_anywhere_even_in_an_auto_enrol_jurisdiction(client, world, db):
    """DIAAT-373 decision 1: no auto-enrol, whatever the old column says."""
    with Session(db) as session:
        iac = session.get(type(world["iac"]), world["iac"].id)
        iac.auto_enrol = True
        session.add(iac)
        session.commit()
    me = Person(client).sign_in().json()["me"]
    assert me["status"] == "no_access" and me["assignments"] == []


def test_the_user_with_no_role_reaches_only_the_no_context_endpoints(client, world):
    token = Person(client).sign_in().json()["token"]
    headers = bearer(token)

    me = client.get("/api/me/permissions", headers=headers)
    assert me.status_code == 200 and me.json()["status"] == "no_access"
    assert client.get("/api/jurisdictions/public", headers=headers).status_code == 200
    for path in ("/api/user", "/api/users/me", "/api/jurisdictions/current", "/api/user/onboarding-status"):
        refused = client.get(path, headers=headers)
        assert refused.status_code == 403, path
        assert refused.json()["detail"]["code"] == "context_required", path


def test_the_entra_access_roles_gate_still_guards_the_sign_in_until_it_is_emptied(client, world, db, monkeypatch):
    """DIAAT-386: with AUTH_ACCESS_ROLES set, a caller holding none of those Entra roles is refused at the
    door and no user is created; emptied (as it will be in each environment once the permissions are
    enforced), a tenant member with no role signs in and sees "no access yet"."""
    monkeypatch.setenv("AUTH_ACCESS_ROLES", "Judge,LegalTextManager,SystemAdministrator")
    refused = Person(client, roles=[]).sign_in()
    assert refused.status_code == 403 and "token" not in refused.json()
    with Session(db) as session:
        assert session.exec(select(User)).all() == []
    assert Person(client, roles=["Judge"]).sign_in().status_code == 200

    monkeypatch.setenv("AUTH_ACCESS_ROLES", "")
    assert Person(client, roles=[]).sign_in().json()["me"]["status"] == "no_access"


def test_signing_in_again_does_not_duplicate_or_change_the_user(client, world, db):
    person = Person(client)
    first = person.sign_in().json()["me"]["user"]["id"]
    second = person.sign_in().json()["me"]["user"]["id"]
    assert first == second
    with Session(db) as session:
        assert len(session.exec(select(User)).all()) == 1


def test_the_display_name_follows_the_sign_in(client, world, db):
    person = Person(client)
    person.sign_in()
    person.name = "Judge Renamed"
    assert person.sign_in().json()["me"]["user"]["name"] == "Judge Renamed"
    with Session(db) as session:
        assert session.exec(select(User)).one().name == "Judge Renamed"


# --- choosing the context ---------------------------------------------------------------


def test_one_pair_is_selected_automatically(client, world, db):
    person = user_with(client, db, (world["et"], "judge"))
    response = person.sign_in()
    me = response.json()["me"]

    assert me["status"] == "ready"
    assert me["assignments"] == [{"jurisdiction": {"code": "ET", "name": "ET name"}, "role": "Judge"}]
    assert me["active"] == {"jurisdiction": {"code": "ET", "name": "ET name"}, "role": "Judge"}
    assert "dictation.use" in me["permissions"] and "users.manage" not in me["permissions"]
    claims = claims_of(response.json()["token"])
    assert claims["ctx"] == {"jurisdiction": "ET", "role": "Judge"}
    assert claims["assignments"] == [{"jurisdiction": "ET", "role": "Judge"}]


def test_a_single_global_super_administrator_pair_is_selected_with_no_jurisdiction(client, world, db):
    person = user_with(client, db, (None, SUPER))
    me = person.sign_in().json()["me"]
    assert me["status"] == "ready"
    assert me["active"] == {"jurisdiction": None, "role": SUPER}
    assert "jurisdiction.create" in me["permissions"]


def test_two_pairs_must_be_chosen_and_the_context_is_null_until_then(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (world["iac"], "admin"))
    response = person.sign_in()
    me = response.json()["me"]

    assert me["status"] == "choose"
    assert me["active"] is None and me["permissions"] == []
    assert [(a["jurisdiction"]["code"], a["role"]) for a in me["assignments"]] == [
        ("ET", "Judge"),
        ("IAC", ADMIN),
    ]
    claims = claims_of(response.json()["token"])
    assert claims["ctx"] is None
    assert len(claims["assignments"]) == 2
    # And until they choose, every ordinary route is refused.
    refused = client.get("/api/user", headers=bearer(response.json()["token"]))
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "context_required"


def test_two_roles_in_one_jurisdiction_are_two_pairs(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (world["et"], "LegalTextManager"))
    assert person.sign_in().json()["me"]["status"] == "choose"


def test_choosing_a_pair_gives_a_token_acting_in_it(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (world["iac"], "admin"))
    token = person.sign_in().json()["token"]

    chosen = client.put(
        "/api/me/context", json={"jurisdiction": "iac", "role": ADMIN}, headers=bearer(token)
    )

    assert chosen.status_code == 200
    body = chosen.json()
    assert body["me"]["status"] == "ready"
    assert body["me"]["active"]["jurisdiction"]["code"] == "IAC" and body["me"]["active"]["role"] == ADMIN
    assert "users.manage" in body["me"]["permissions"] and "dictation.use" not in body["me"]["permissions"]
    assert claims_of(body["token"])["ctx"] == {"jurisdiction": "IAC", "role": ADMIN}
    # The new token works where the old could not.
    assert client.get("/api/jurisdictions/current", headers=bearer(body["token"])).status_code == 200
    assert client.get("/api/jurisdictions/current", headers=bearer(token)).status_code == 403


def test_the_permissions_are_those_of_the_active_role_only_not_the_union(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (world["iac"], "admin"))
    token = person.sign_in().json()["token"]

    judge = client.put("/api/me/context", json={"jurisdiction": "ET", "role": "Judge"}, headers=bearer(token)).json()
    admin = client.put("/api/me/context", json={"jurisdiction": "IAC", "role": ADMIN}, headers=bearer(token)).json()

    assert "dictation.use" in judge["me"]["permissions"] and "users.manage" not in judge["me"]["permissions"]
    assert "users.manage" in admin["me"]["permissions"] and "dictation.use" not in admin["me"]["permissions"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"jurisdiction": "IAC", "role": "Judge"}, id="a-role-held-in-another-jurisdiction"),
        pytest.param({"jurisdiction": "ET", "role": ADMIN}, id="another-role-in-the-jurisdiction"),
        pytest.param({"jurisdiction": None, "role": "Judge"}, id="a-judge-with-no-jurisdiction"),
        pytest.param({"jurisdiction": None, "role": SUPER}, id="super-administrator-not-held"),
        pytest.param({"jurisdiction": "XX", "role": "Judge"}, id="no-such-jurisdiction"),
        pytest.param({"jurisdiction": "ET", "role": "SystemAdministrator"}, id="system-administrator"),
        pytest.param({"jurisdiction": None, "role": "SystemAdministrator"}, id="global-system-administrator"),
        pytest.param({"jurisdiction": "ET", "role": "Normal"}, id="normal"),
    ],
)
def test_choosing_a_pair_not_held_is_403_and_changes_nothing(client, world, db, body):
    person = user_with(client, db, (world["et"], "judge"), (world["iac"], "admin"), roles=["SystemAdministrator"])
    token = person.sign_in().json()["token"]
    with Session(db) as session:
        before = sorted(str(r.id) for r in session.exec(select(RoleAssignment)).all())

    refused = client.put("/api/me/context", json=body, headers=bearer(token))

    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "context_not_available"
    assert "token" not in refused.json()
    with Session(db) as session:
        assert sorted(str(r.id) for r in session.exec(select(RoleAssignment)).all()) == before
    # The token the user has is as it was: still no context.
    me = client.get("/api/me/permissions", headers=bearer(token)).json()
    assert me["status"] == "choose" and me["active"] is None


def test_the_context_body_needs_both_fields(client, world, db):
    person = user_with(client, db, (world["et"], "judge"))
    token = person.sign_in().json()["token"]
    assert client.put("/api/me/context", json={"role": "Judge"}, headers=bearer(token)).status_code == 422
    assert client.put("/api/me/context", json={"jurisdiction": "ET"}, headers=bearer(token)).status_code == 422


def test_an_assignment_in_a_disabled_jurisdiction_is_not_offered(client, world, db):
    off = add_jurisdiction(db, "OFF", enabled=False)
    person = user_with(client, db, (world["et"], "judge"), (off, "judge"))
    me = person.sign_in().json()["me"]
    # One pair is left, so it is selected.
    assert [a["jurisdiction"]["code"] for a in me["assignments"]] == ["ET"] and me["status"] == "ready"


# --- renewing the token -----------------------------------------------------------------


def test_a_renewal_keeps_the_context_the_session_chose_when_it_is_still_held(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (world["iac"], "admin"))
    renewed = person.sign_in(requested_context=f"IAC:{ADMIN}")
    assert renewed.json()["me"]["active"]["jurisdiction"]["code"] == "IAC"
    assert claims_of(renewed.json()["token"])["ctx"] == {"jurisdiction": "IAC", "role": ADMIN}


def test_a_renewal_names_a_global_context_with_a_dash(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (None, SUPER))
    renewed = person.sign_in(requested_context=f"-:{SUPER}")
    assert claims_of(renewed.json()["token"])["ctx"] == {"jurisdiction": None, "role": SUPER}


@pytest.mark.parametrize("requested", ["IAC:Judge", "ET:JurisdictionAdministrator", "-:Judge", "nonsense", ":", "ET:", ""])
def test_a_renewal_naming_a_context_no_longer_held_has_none(client, world, db, requested):
    """Not the auto rule either: a user who had chosen something they have lost chooses again."""
    person = user_with(client, db, (world["et"], "judge"))
    renewed = person.sign_in(requested_context=requested)
    assert claims_of(renewed.json()["token"])["ctx"] is None
    assert renewed.json()["me"]["status"] == "choose"


def test_a_role_removed_while_active_is_gone_at_the_next_token(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (world["iac"], "admin"))
    token = person.sign_in(requested_context="ET:Judge").json()["token"]
    with Session(db) as session:
        for row in session.exec(select(RoleAssignment).where(RoleAssignment.role == "Judge")).all():
            session.delete(row)
        session.commit()

    # The token in hand still says what it said (no database read to authorise: DIAAT-373) ...
    assert client.get("/api/jurisdictions/current", headers=bearer(token)).status_code == 200
    # ... and the renewal, which keeps the context only if it is held, drops it.
    renewed = person.sign_in(requested_context="ET:Judge").json()
    assert renewed["me"]["active"] is None
    assert [a["role"] for a in renewed["me"]["assignments"]] == [ADMIN]


# --- SystemAdministrator ----------------------------------------------------------------


def test_a_system_administrator_with_no_assignments_is_ready_for_the_system_pages(client, world):
    person = Person(client, roles=["SystemAdministrator"])
    response = person.sign_in()
    me = response.json()["me"]

    assert me["status"] == "ready"
    assert me["assignments"] == [] and me["active"] is None
    assert set(me["permissions"]) == {
        "admin.access",
        "feature_flags.manage",
        "system.settings",
        "security.alert_test",
        "jurisdiction_super_admin.assign",
        "jurisdiction_admin.assign",
    }
    claims = claims_of(response.json()["token"])
    assert claims["sysadmin"] is True and claims["ctx"] is None and claims["assignments"] == []


def test_system_administrator_is_never_an_assignment_or_a_context(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), roles=["SystemAdministrator"])
    response = person.sign_in()
    body = response.json()

    assert claims_of(body["token"])["sysadmin"] is True
    assert "SystemAdministrator" not in str(body["me"]["assignments"])
    assert body["me"]["active"] == {"jurisdiction": {"code": "ET", "name": "ET name"}, "role": "Judge"}
    assert "SystemAdministrator" not in str(claims_of(body["token"])["assignments"])
    assert claims_of(body["token"])["ctx"] == {"jurisdiction": "ET", "role": "Judge"}
    # Added to the active role's permissions.
    assert {"dictation.use", "feature_flags.manage"} <= set(body["me"]["permissions"])


def test_a_system_administrator_with_a_pair_to_choose_keeps_the_system_permissions_with_no_context(client, world, db):
    person = user_with(
        client, db, (world["et"], "judge"), (world["iac"], "admin"), roles=["SystemAdministrator"]
    )
    me = person.sign_in().json()["me"]
    assert me["status"] == "choose" and me["active"] is None
    assert "feature_flags.manage" in me["permissions"] and "dictation.use" not in me["permissions"]


def test_the_system_administrator_flag_comes_only_from_the_entra_roles_claim(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), roles=["SystemAdministrator"])
    assert claims_of(person.sign_in().json()["token"])["sysadmin"] is True
    # Signing in without the Entra role, the same user is not one, whatever happened before.
    person.roles = ["Judge", "LegalTextManager"]
    after = person.sign_in().json()
    assert claims_of(after["token"])["sysadmin"] is False
    assert "feature_flags.manage" not in after["me"]["permissions"]


def test_the_flag_is_carried_over_when_the_user_chooses_a_context(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (world["iac"], "admin"), roles=["SystemAdministrator"])
    token = person.sign_in().json()["token"]
    chosen = client.put("/api/me/context", json={"jurisdiction": "ET", "role": "Judge"}, headers=bearer(token))
    assert claims_of(chosen.json()["token"])["sysadmin"] is True


# --- the pending email is claimed at the first sign-in ----------------------------------


def test_what_was_assigned_to_an_email_is_claimed_at_first_sign_in_whatever_its_case(client, world, db):
    from transcribe_api.domain.assignments import add_assignment

    with Session(db) as session:
        add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, email="  New.Judge@Example.TEST ")
        session.commit()
        pending = session.exec(select(RoleAssignment)).one()
        assert pending.pending_email == "new.judge@example.test" and pending.user_id is None

    person = Person(client, email="NEW.JUDGE@example.test")
    me = person.sign_in().json()["me"]

    assert me["status"] == "ready"
    assert me["active"]["jurisdiction"]["code"] == "ET" and me["active"]["role"] == "Judge"
    with Session(db) as session:
        row = session.exec(select(RoleAssignment)).one()
        assert row.user_id == UUID(me["user"]["id"]) and row.pending_email is None


def test_a_pending_assignment_of_another_email_is_not_claimed(client, world, db):
    from transcribe_api.domain.assignments import add_assignment

    with Session(db) as session:
        add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, email="someone.else@example.test")
        session.commit()
    me = Person(client, email="newcomer@example.test").sign_in().json()["me"]
    assert me["status"] == "no_access"
    with Session(db) as session:
        assert session.exec(select(RoleAssignment)).one().user_id is None


# --- deactivation -----------------------------------------------------------------------


def test_a_deactivated_user_is_refused_and_not_recreated_or_reenabled(client, world, db):
    person = user_with(client, db, (world["et"], "judge"))
    token = person.sign_in().json()["token"]
    with Session(db) as session:
        user = session.get(User, person.user_id)
        user.is_active = False
        session.add(user)
        session.commit()

    refused = person.sign_in()
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "user_deactivated"
    assert "token" not in refused.json()
    with Session(db) as session:
        users = session.exec(select(User)).all()
        assert len(users) == 1 and users[0].is_active is False
    # A token minted before is refused too: the user row is read for the request anyway.
    for path in ("/api/user", "/api/me/permissions"):
        response = client.get(path, headers=bearer(token))
        assert response.status_code == 403 and response.json()["detail"]["code"] == "user_deactivated", path


def test_a_token_for_a_user_who_is_not_in_the_database_is_401(client, world):
    forged = af.forged_app_token(
        {"ctx": {"jurisdiction": "IAC", "role": "Judge"}, "assignments": [{"jurisdiction": "IAC", "role": "Judge"}]}
    )
    assert client.get("/api/user", headers=bearer(forged)).status_code == 401


# --- which token goes where -------------------------------------------------------------


def test_the_entra_token_is_rejected_on_ordinary_routes(client, world):
    person = Person(client)
    entra = person.entra()
    for path in ("/api/user", "/api/me/permissions", "/api/jurisdictions/current", "/api/jurisdictions/public"):
        response = client.get(path, headers=bearer(entra))
        assert response.status_code == 401, path
    assert client.put("/api/me/context", json={"jurisdiction": "ET", "role": "Judge"}, headers=bearer(entra)).status_code == 401
    assert client.delete("/api/me/jurisdictions/ET", headers=bearer(entra)).status_code == 401


def test_the_app_token_is_rejected_on_the_sign_in(client, world):
    token = Person(client).sign_in().json()["token"]
    for headers in (bearer(token), {}):
        assert client.post("/api/auth/session", headers=headers).status_code == 401


def test_no_token_is_401_with_a_challenge(client, world):
    response = client.get("/api/me/permissions")
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("expired", id="expired"),
        pytest.param("tampered", id="tampered"),
        pytest.param("wrong-audience", id="wrong-audience"),
        pytest.param("wrong-issuer", id="wrong-issuer"),
        pytest.param("wrong-key", id="wrong-key"),
        pytest.param("none-algorithm", id="no-signature"),
    ],
)
def test_an_invalid_app_token_is_401_on_every_kind_of_route(client, world, db, case):
    person = user_with(client, db, (world["et"], "judge"))
    ctx = {"jurisdiction": "ET", "role": "Judge"}
    claims = {"sub": str(person.user_id), "ctx": ctx, "assignments": [ctx]}
    if case == "expired":
        token = af.forged_app_token(claims | {"exp": int(time.time()) - 10})
    elif case == "wrong-audience":
        token = af.forged_app_token(claims | {"aud": "other"})
    elif case == "wrong-issuer":
        token = af.forged_app_token(claims | {"iss": "somebody"})
    elif case == "wrong-key":
        token = af.forged_app_token(claims, key="another-key-another-key-another-key-0000")
    elif case == "none-algorithm":
        import jwt

        token = jwt.encode(claims | {"aud": "benchnotes", "iss": "benchnotes-api"}, None, algorithm="none")
    else:  # tampered: a good token whose payload was swapped for one that is not signed for
        good = person.sign_in().json()["token"]
        header, _, signature = good.split(".")
        payload = af.forged_app_token(claims | {"sysadmin": True}).split(".")[1]
        token = f"{header}.{payload}.{signature}"
    for path in ("/api/user", "/api/me/permissions", "/api/jurisdictions/current"):
        assert client.get(path, headers=bearer(token)).status_code == 401, path


def test_every_401_is_in_the_apis_error_shape_with_a_challenge(client, world, db):
    """Integration finding: the 401s had a plain-string ``detail`` where every other refusal has ``{code, message}``."""
    person = user_with(client, db, (world["et"], "judge"))
    unknown = af.forged_app_token({"sub": str(uuid4())})
    cases = {
        "not_authenticated": client.get("/api/me/permissions"),
        "invalid_token": client.get("/api/me/permissions", headers=bearer("not-a-token")),
        "unknown_user": client.get("/api/me/permissions", headers=bearer(unknown)),
    }
    for code, response in cases.items():
        assert response.status_code == 401, code
        assert response.json()["detail"]["code"] == code and response.json()["detail"]["message"]
        assert response.headers["www-authenticate"] == "Bearer"
    # The sign-in's own 401s too: no bearer, an unverifiable one.
    assert client.post("/api/auth/session").json()["detail"]["code"] == "not_authenticated"
    assert person  # (signed in above only to have a database with a user in it)


def test_the_context_in_a_valid_token_is_trusted_without_reading_the_assignments(client, world, db):
    """DIAAT-373: no database read authorises a request, so a token minted for a context is good for
    it until it expires, whatever the assignments say now."""
    person = user_with(client, db, (world["et"], "judge"))
    token = person.sign_in().json()["token"]
    with Session(db) as session:
        session.delete(session.exec(select(RoleAssignment)).one())
        session.commit()
    assert client.get("/api/jurisdictions/current", headers=bearer(token)).status_code == 200


# --- the tab's advisory headers ---------------------------------------------------------


def _acting(client, db, world, *, pair=("et", "judge")):
    person = user_with(client, db, (world[pair[0]], pair[1]))
    return person.sign_in().json()["token"]


def test_a_matching_or_absent_context_header_is_fine(client, world, db):
    token = _acting(client, db, world)
    assert client.get("/api/jurisdictions/current", headers=bearer(token)).status_code == 200
    assert (
        client.get("/api/jurisdictions/current", headers=bearer(token, **{"X-Jurisdiction": "ET", "X-Role": "Judge"}))
    ).status_code == 200
    assert client.get("/api/jurisdictions/current", headers=bearer(token, **{"X-Jurisdiction": "et"})).status_code == 200
    assert client.get("/api/jurisdictions/current", headers=bearer(token, **{"X-Role": "Judge"})).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Jurisdiction": "IAC"},
        {"X-Jurisdiction": "-"},
        {"X-Role": ADMIN},
        {"X-Jurisdiction": "ET", "X-Role": ADMIN},
        {"X-Jurisdiction": "IAC", "X-Role": "Judge"},
        {"X-Jurisdiction": ""},
    ],
)
def test_a_context_header_that_differs_from_the_tokens_is_409_context_changed(client, world, db, headers):
    token = _acting(client, db, world)
    for path in ("/api/jurisdictions/current", "/api/user"):
        response = client.get(path, headers=bearer(token, **headers))
        assert response.status_code == 409, path
        assert response.json()["detail"]["code"] == "context_changed", path


def test_a_global_context_matches_a_dash(client, world, db):
    person = user_with(client, db, (None, SUPER))
    token = person.sign_in().json()["token"]
    assert client.get("/api/user", headers=bearer(token, **{"X-Jurisdiction": "-", "X-Role": SUPER})).status_code == 200
    assert client.get("/api/user", headers=bearer(token, **{"X-Jurisdiction": "ET"})).status_code == 409


def test_a_stale_tab_can_still_ask_who_it_is_and_switch(client, world, db):
    """The advisory headers are not checked on the no-context endpoints: that is how a stale tab recovers."""
    person = user_with(client, db, (world["et"], "judge"), (world["iac"], "admin"))
    token = person.sign_in(requested_context="ET:Judge").json()["token"]
    stale = {"X-Jurisdiction": "IAC", "X-Role": ADMIN}
    assert client.get("/api/me/permissions", headers=bearer(token, **stale)).status_code == 200
    switched = client.put("/api/me/context", json={"jurisdiction": "IAC", "role": ADMIN}, headers=bearer(token, **stale))
    assert switched.status_code == 200


# --- the permission checks follow the active role (DIAAT-376) ---


def test_the_permission_checks_follow_the_active_role(client, world, db, mocker):
    mocker.patch("transcribe_api.api.routes_dictation.fetch_transcriptions_metadata", return_value=[])
    person = user_with(
        client,
        db,
        (world["et"], "judge"),
        (world["iac"], "admin"),
        (world["iac"], "LegalTextManager"),
        (None, SUPER),
    )
    token = person.sign_in().json()["token"]

    def acting(jurisdiction, role):
        chosen = client.put("/api/me/context", json={"jurisdiction": jurisdiction, "role": role}, headers=bearer(token))
        assert chosen.status_code == 200
        return bearer(chosen.json()["token"])

    # Dictation routes: a Judge only (dictation.use), not any other role.
    assert client.get("/api/transcriptions-metadata", headers=acting("ET", "Judge")).status_code == 200
    for jurisdiction, role in (("IAC", "LegalTextManager"), ("IAC", ADMIN), (None, SUPER)):
        refused = client.get("/api/transcriptions-metadata", headers=acting(jurisdiction, role))
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "permission_denied", role
    # The admin area's landing route: those with admin.access, not the judge.
    assert client.get("/api/admin/me", headers=acting("IAC", "LegalTextManager")).status_code == 200
    assert client.get("/api/admin/me", headers=acting("ET", "Judge")).status_code == 403


def test_a_system_administrator_reaches_the_system_routes_with_no_context_and_nothing_else(client, world, db):
    person = Person(client, roles=["SystemAdministrator"])
    token = person.sign_in().json()["token"]
    headers = bearer(token)

    # System-level: the Entra role is enough, with no assignment and no context.
    assert client.get("/api/admin/me", headers=headers).status_code == 200
    assert client.get("/api/flags", headers=headers).status_code == 200
    # The security alert test passes the permission (and is then 404: not enabled in the tests).
    assert client.post("/api/security/alert-test", headers=headers).status_code == 404
    # Not jurisdiction material: there is no jurisdiction to act in, and no permission for it.
    refused = client.get("/api/jurisdictions/current", headers=headers)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "context_required"
    assert client.get("/api/transcriptions-metadata", headers=headers).status_code == 403
    assert client.get("/api/admin/jurisdictions", headers=headers).status_code == 403


def test_the_system_routes_are_refused_to_everyone_without_the_entra_role(client, world, db):
    person = user_with(client, db, (world["et"], "judge"))
    token = person.sign_in().json()["token"]
    refused = client.post("/api/security/alert-test", headers=bearer(token))
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "permission_denied"


# --- the jurisdiction of the context ----------------------------------------------------


def test_the_current_jurisdiction_is_the_contexts(client, world, db):
    token = _acting(client, db, world, pair=("iac", "judge"))
    body = client.get("/api/jurisdictions/current", headers=bearer(token)).json()
    assert body["code"] == "IAC" and body["name"] == "Immigration and Asylum Chamber"
    assert body["template"] == "iac_decision" and body["features"] == {"hearing_recordings": True}
    assert "home.intro" in body["wording"]


def test_a_global_role_has_no_jurisdiction_to_work_in(client, world, db):
    person = user_with(client, db, (None, SUPER))
    token = person.sign_in().json()["token"]
    response = client.get("/api/jurisdictions/current", headers=bearer(token))
    assert response.status_code == 403 and response.json()["detail"]["code"] == "no_jurisdiction"


def test_a_context_in_a_jurisdiction_disabled_since_is_403(client, world, db):
    token = _acting(client, db, world)
    with Session(db) as session:
        et = session.get(type(world["et"]), world["et"].id)
        et.enabled = False
        session.add(et)
        session.commit()
    response = client.get("/api/jurisdictions/current", headers=bearer(token))
    assert response.status_code == 403 and response.json()["detail"]["code"] == "jurisdiction_not_available"


def test_the_public_jurisdictions_are_the_enabled_ones_by_name(client, world, db):
    add_jurisdiction(db, "OFF", enabled=False)
    token = Person(client).sign_in().json()["token"]
    assert client.get("/api/jurisdictions/public", headers=bearer(token)).json() == [
        {"code": "ET", "name": "ET name", "roles": ["Judge", "LegalTextManager"]},
        {"code": "IAC", "name": "Immigration and Asylum Chamber", "roles": ["Judge", "LegalTextManager"]},
    ]


def test_the_old_per_user_jurisdiction_endpoints_are_gone(client, world, db):
    token = _acting(client, db, world)
    assert client.get("/api/jurisdictions/me", headers=bearer(token)).status_code == 404
    assert client.put("/api/jurisdictions/active", json={"code": "ET"}, headers=bearer(token)).status_code in (404, 405)


# --- leaving a jurisdiction -------------------------------------------------------------


def _assignments(db):
    with Session(db) as session:
        return sorted((r.jurisdiction_id, r.role) for r in session.exec(select(RoleAssignment)).all() if r.user_id)


def test_leaving_a_jurisdiction_deletes_only_that_jurisdictions_assignments(client, world, db):
    stranger = user_with(client, db, (world["et"], "judge"))
    person = user_with(
        client,
        db,
        (world["et"], "judge"),
        (world["et"], "LegalTextManager"),
        (world["iac"], "admin"),
        (None, SUPER),
    )
    token = person.sign_in(requested_context="IAC:JurisdictionAdministrator").json()["token"]

    response = client.delete("/api/me/jurisdictions/et", headers=bearer(token))

    assert response.status_code == 200
    with Session(db) as session:
        mine = [(r.jurisdiction_id, r.role) for r in session.exec(select(RoleAssignment)).all() if r.user_id == person.user_id]
        theirs = [r.role for r in session.exec(select(RoleAssignment)).all() if r.user_id == stranger.user_id]
    assert sorted(mine, key=str) == sorted([(world["iac"].id, ADMIN), (None, SUPER)], key=str)
    assert theirs == ["Judge"]  # nobody else's
    me = response.json()["me"]
    # The global role first (by jurisdiction name: none), then the IAC's.
    assert [(a["jurisdiction"] and a["jurisdiction"]["code"], a["role"]) for a in me["assignments"]] == [
        (None, SUPER),
        ("IAC", ADMIN),
    ]
    # They were acting in the IAC, which they have not left: the context is kept.
    assert me["active"]["jurisdiction"]["code"] == "IAC"
    assert claims_of(response.json()["token"])["ctx"] == {"jurisdiction": "IAC", "role": ADMIN}


def test_leaving_the_active_jurisdiction_means_choosing_again(client, world, db):
    person = user_with(
        client, db, (world["et"], "judge"), (world["iac"], "admin"), (None, SUPER)
    )
    token = person.sign_in(requested_context="ET:Judge").json()["token"]
    response = client.delete("/api/me/jurisdictions/ET", headers=bearer(token))
    assert response.status_code == 200
    me = response.json()["me"]
    assert me["status"] == "choose" and me["active"] is None
    assert claims_of(response.json()["token"])["ctx"] is None
    assert [a["role"] for a in me["assignments"]] == [SUPER, ADMIN]


def test_leaving_the_active_jurisdiction_with_one_pair_left_selects_it(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (world["iac"], "admin"))
    token = person.sign_in(requested_context="ET:Judge").json()["token"]
    response = client.delete("/api/me/jurisdictions/ET", headers=bearer(token))
    assert response.json()["me"]["status"] == "ready"
    assert claims_of(response.json()["token"])["ctx"] == {"jurisdiction": "IAC", "role": ADMIN}


def test_leaving_the_last_jurisdiction_leaves_no_access(client, world, db):
    person = user_with(client, db, (world["et"], "judge"))
    token = person.sign_in().json()["token"]
    response = client.delete("/api/me/jurisdictions/ET", headers=bearer(token))
    assert response.json()["me"]["status"] == "no_access"
    assert claims_of(response.json()["token"])["ctx"] is None


def test_leaving_from_the_choose_screen_works_with_no_context(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (world["iac"], "admin"), (None, SUPER))
    token = person.sign_in().json()["token"]
    response = client.delete("/api/me/jurisdictions/IAC", headers=bearer(token))
    assert response.status_code == 200 and response.json()["me"]["status"] == "choose"


def test_leaving_a_jurisdiction_is_audited_with_the_context_it_was_done_in(client, world, db):
    person = user_with(client, db, (world["et"], "judge"), (world["et"], "LegalTextManager"), (world["iac"], "admin"))
    token = person.sign_in(requested_context=f"IAC:{ADMIN}").json()["token"]

    client.delete("/api/me/jurisdictions/ET", headers=bearer(token))

    with Session(db) as session:
        [row] = session.exec(select(AuditLog).where(AuditLog.event_type == "membership.leave")).all()
    assert row.user_id == str(person.user_id) and row.outcome == "success"
    assert row.object_type == "jurisdiction" and row.object_id == str(world["et"].id)
    assert row.detail == {"code": "ET", "roles": ["Judge", "LegalTextManager"]}
    # Acting as the IAC administrator when they left the ET.
    assert row.active_role == ADMIN and row.active_jurisdiction_id == world["iac"].id


def test_leaving_a_jurisdiction_you_hold_nothing_in_is_404_and_audits_nothing(client, world, db):
    person = user_with(client, db, (world["et"], "judge"))
    token = person.sign_in().json()["token"]
    for code in ("IAC", "XX"):
        response = client.delete(f"/api/me/jurisdictions/{code}", headers=bearer(token))
        assert response.status_code == 404 and response.json()["detail"]["code"] == "not_a_member"
    with Session(db) as session:
        assert session.exec(select(AuditLog).where(AuditLog.event_type == "membership.leave")).all() == []
    assert len(_assignments(db)) == 1


# --- local dev personas -----------------------------------------------------------------


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local")


def seed_personas(db):
    """What scripts-seed-personas.py does, into the test database."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts-seed-personas.py"
    spec = importlib.util.spec_from_file_location("scripts_seed_personas", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Session(db) as session:
        module.seed(session)
        session.commit()


def persona(client, name, **headers):
    return client.post("/api/auth/session", headers={"X-Dev-Persona": name} | headers)


@pytest.mark.parametrize(
    ("name", "status", "pairs", "sysadmin"),
    [
        ("judge-et", "ready", [("ET", "Judge")], False),
        ("judge-iac", "ready", [("IAC", "Judge")], False),
        ("multi", "choose", [("ET", "Judge"), ("IAC", ADMIN)], False),
        ("jadmin-iac", "ready", [("IAC", ADMIN)], False),
        ("jadmin-et", "ready", [("ET", ADMIN)], False),
        ("legal-iac", "ready", [("IAC", "LegalTextManager")], False),
        ("jsuper", "ready", [(None, SUPER)], False),
        ("sysadmin", "ready", [], True),
        ("no-role", "no_access", [], False),
    ],
)
def test_each_persona_signs_in_as_the_contract_says(client, db, local, name, status, pairs, sysadmin):
    seed_personas(db)
    response = persona(client, name)

    assert response.status_code == 200, response.text
    me = response.json()["me"]
    assert me["status"] == status
    assert [(a["jurisdiction"] and a["jurisdiction"]["code"], a["role"]) for a in me["assignments"]] == pairs
    assert me["user"]["email"] == f"{name}@localhost.test"
    assert claims_of(response.json()["token"])["sysadmin"] is sysadmin
    assert ("feature_flags.manage" in me["permissions"]) is sysadmin


def test_a_persona_is_refused_outside_local_mode(client, db, monkeypatch):
    seed_personas(db)
    for environment in ("test", "production", "dev", "aat", "prod", ""):
        monkeypatch.setenv("ENVIRONMENT", environment)
        response = persona(client, "sysadmin")
        assert response.status_code == 401, environment
        assert response.json()["detail"]["code"] == "dev_persona_disabled"
        assert "token" not in response.json()


def test_a_persona_header_is_refused_outside_local_mode_even_with_a_real_token(client, db, monkeypatch):
    seed_personas(db)
    monkeypatch.setenv("ENVIRONMENT", "production")
    response = persona(client, "jsuper", Authorization=f"Bearer {af.entra_token()}")
    assert response.status_code == 401


def test_an_unknown_persona_is_400_and_one_not_seeded_is_409(client, db, local):
    assert persona(client, "nobody").status_code == 400
    # judge-et has assignments, which only the seed script creates.
    unseeded = persona(client, "judge-et")
    assert unseeded.status_code == 409 and unseeded.json()["detail"]["code"] == "persona_not_seeded"


def test_a_persona_needs_no_seeding_when_it_has_no_assignments(client, db, local):
    assert persona(client, "no-role").json()["me"]["status"] == "no_access"
    assert persona(client, "sysadmin").json()["me"]["status"] == "ready"


def test_local_mode_without_a_persona_or_token_is_401(client, db, local):
    assert client.post("/api/auth/session").status_code == 401
    # No "everything" bypass any more: a route reached with no token is 401 in local mode too.
    assert client.get("/api/user").status_code == 401


def test_a_persona_goes_through_the_same_mint_and_renewal(client, db, local):
    seed_personas(db)
    first = persona(client, "multi")
    assert first.json()["me"]["status"] == "choose"
    renewed = persona(client, "multi", **{"X-Requested-Context": f"IAC:{ADMIN}"})
    assert claims_of(renewed.json()["token"])["ctx"] == {"jurisdiction": "IAC", "role": ADMIN}


def test_a_deactivated_persona_is_refused_too(client, db, local):
    seed_personas(db)
    with Session(db) as session:
        user = session.exec(select(User).where(User.email == "judge-et@localhost.test")).one()
        user.is_active = False
        session.add(user)
        session.commit()
    assert persona(client, "judge-et").status_code == 403


# --- audit stamping ---------------------------------------------------------------------


def test_an_audit_row_written_in_a_request_carries_its_context(client, world, db):
    from transcribe_api.domain.audit import record_audit
    from transcribe_api.domain.auth.context import set_audit_context
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    token = _acting(client, db, world, pair=("iac", "judge"))

    # Any route that audits would do; a route here records one of its own.
    @client.app.get("/api/_audit_probe")
    def probe(current_user=Depends(get_current_user)):
        record_audit("jurisdiction.edit", actor_id=current_user.id, outcome="success", object_type="jurisdiction")
        return {}

    assert client.get("/api/_audit_probe", headers=bearer(token)).status_code == 200
    with Session(db) as session:
        row = session.exec(select(AuditLog).where(AuditLog.event_type == "jurisdiction.edit")).one()
    assert row.active_role == "Judge" and row.active_jurisdiction_id == world["iac"].id
    # Outside a request there is no context to stamp.
    set_audit_context(None)
    record_audit("jurisdiction.edit", actor_id=uuid4(), outcome="success")
    with Session(db) as session:
        stamped = [r for r in session.exec(select(AuditLog)).all() if r.user_id != str(row.user_id)]
    assert [(r.active_role, r.active_jurisdiction_id) for r in stamped] == [(None, None)]


def test_a_global_role_stamps_its_role_and_no_jurisdiction(client, world, db):
    from transcribe_api.domain.audit import record_audit
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    person = user_with(client, db, (None, SUPER))
    token = person.sign_in().json()["token"]

    @client.app.get("/api/_audit_probe")
    def probe(current_user=Depends(get_current_user)):
        record_audit("jurisdiction.edit", actor_id=current_user.id, outcome="success")
        return {}

    client.get("/api/_audit_probe", headers=bearer(token))
    with Session(db) as session:
        row = session.exec(select(AuditLog).where(AuditLog.event_type == "jurisdiction.edit")).one()
    assert row.active_role == SUPER and row.active_jurisdiction_id is None


def test_the_response_to_a_token_is_never_cached(client, world, db):
    person = user_with(client, db, (world["et"], "judge"))
    response = person.sign_in()
    assert response.headers["cache-control"] == "no-store"
    token = response.json()["token"]
    assert client.get("/api/me/permissions", headers=bearer(token)).headers["cache-control"] == "no-store"
    assert (
        client.put("/api/me/context", json={"jurisdiction": "ET", "role": "Judge"}, headers=bearer(token)).headers[
            "cache-control"
        ]
        == "no-store"
    )


def test_the_token_lifetime_is_the_setting(client, world, db, monkeypatch):
    from transcribe_api.runtime.settings_dictation import get_settings

    monkeypatch.setenv("APP_JWT_TTL_SECONDS", "120")
    get_settings.cache_clear()
    response = Person(client).sign_in()
    claims = claims_of(response.json()["token"])
    assert claims["exp"] - claims["iat"] == 120
    assert app_token.verify(response.json()["token"]).expires_at.isoformat().startswith(response.json()["expires_at"][:19])


def test_the_recording_area_checks_the_tabs_context_headers_too(client, world, db, monkeypatch):
    """/api/v1/* authenticates through its own dependency (dependencies_recording), which is the same
    token check: a tab out of step is 409 there as well, and nothing without a context gets in."""
    from fastapi import Depends

    from transcribe_api.domain.auth import dependencies_recording

    monkeypatch.setattr(dependencies_recording, "get_engine", lambda: db)

    @client.app.get("/api/v1/_probe")
    def probe(current_user=Depends(dependencies_recording.get_current_user)):
        return {"role": current_user.ctx.role}

    person = user_with(client, db, (world["et"], "judge"))
    token = person.sign_in().json()["token"]
    ok = client.get("/api/v1/_probe", headers=bearer(token, **{"X-Jurisdiction": "ET", "X-Role": "Judge"}))
    assert ok.status_code == 200 and ok.json() == {"role": "Judge"}
    for headers in ({"X-Jurisdiction": "IAC"}, {"X-Role": ADMIN}):
        stale = client.get("/api/v1/_probe", headers=bearer(token, **headers))
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "context_changed"
    none = Person(client).sign_in().json()["token"]
    assert client.get("/api/v1/_probe", headers=bearer(none)).json()["detail"]["code"] == "context_required"


# --- where personas and the signing-key exemption apply (review finding S-low (c)) ---------


@pytest.fixture
def fresh_settings():
    """The settings are cached: read again for the test, and again for the tests after it."""
    from transcribe_api.runtime import settings_dictation, settings_recording

    for module in (settings_dictation, settings_recording):
        module.get_settings.cache_clear()
    yield
    for module in (settings_dictation, settings_recording):
        module.get_settings.cache_clear()


def test_a_persona_is_also_refused_against_a_database_that_is_not_local(client, db, local, monkeypatch, fresh_settings):
    """A developer who runs ENVIRONMENT=local against a shared database must not get a SystemAdministrator
    from an unproven header (the seed script refuses such a database too)."""
    import os

    from transcribe_api.runtime import settings_recording

    seed_personas(db)
    local_url = os.environ["DATABASE_CONNECTION_STRING"]
    monkeypatch.setenv("DATABASE_CONNECTION_STRING", "postgresql://u:p@shared-db.example.com:5432/benchnotes")  # pragma: allowlist secret
    settings_recording.get_settings.cache_clear()
    refused = persona(client, "sysadmin")
    assert refused.status_code == 401 and refused.json()["detail"]["code"] == "dev_persona_disabled"
    assert "token" not in refused.json()
    monkeypatch.setenv("DATABASE_CONNECTION_STRING", local_url)
    settings_recording.get_settings.cache_clear()
    assert persona(client, "no-role").status_code == 200  # a local database again


@pytest.mark.parametrize("spelling", ["LOCAL", "Local", " local "])
def test_the_environment_is_read_without_regard_to_case_by_the_persona_check_and_the_settings(
    client, db, monkeypatch, spelling, fresh_settings
):
    """The two used to disagree: ``LOCAL`` enabled personas while the settings treated it as deployed."""
    from transcribe_api.domain.auth.app_token import signing_key_problem
    from transcribe_api.runtime import settings_dictation, settings_recording

    monkeypatch.setenv("ENVIRONMENT", spelling)
    assert persona(client, "no-role").status_code == 200
    assert settings_dictation.get_settings().ENVIRONMENT == "local"
    assert settings_recording.get_settings().ENVIRONMENT == "local"
    assert signing_key_problem(spelling, None) is None  # local needs no key, in any spelling
    assert type(settings_dictation.get_settings()).__name__ == "LocalSettings"


def test_a_deployed_environment_in_any_case_is_still_deployed(monkeypatch):
    from transcribe_api.domain.auth.app_token import signing_key_problem
    from transcribe_api.runtime.settings_dictation import ProductionSettings

    assert ProductionSettings(ENVIRONMENT="PROD").ENVIRONMENT == "prod"
    assert signing_key_problem("AAT", None) == "APP_JWT_SIGNING_KEY is not set"
    assert signing_key_problem("Preview", "short") is not None


def test_an_environment_that_was_never_set_needs_the_signing_key(monkeypatch):
    """The settings default (``local``) is not a decision: a pod that lost ENVIRONMENT must not be taken for a
    developer's machine and start with a signing key of its own (which is different on every replica)."""
    from transcribe_api.domain.auth import app_token
    from transcribe_api.runtime.settings_dictation import LocalSettings

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("APP_JWT_SIGNING_KEY", raising=False)
    unset = LocalSettings(_env_file=None)
    assert unset.ENVIRONMENT == "local" and unset.environment_is_set() is False
    assert app_token.effective_environment(unset) == "unset"
    assert app_token.signing_key_problem(app_token.effective_environment(unset), unset.APP_JWT_SIGNING_KEY) == (
        "APP_JWT_SIGNING_KEY is not set"
    )
    monkeypatch.setattr(app_token, "get_settings", lambda: unset)
    with pytest.raises(RuntimeError, match="APP_JWT_SIGNING_KEY is not set"):
        app_token.derived_key("anything")

    monkeypatch.setenv("ENVIRONMENT", "local")
    explicit = LocalSettings(_env_file=None)
    assert explicit.environment_is_set() is True and app_token.effective_environment(explicit) == "local"
    assert app_token.signing_key_problem("local", None) is None
