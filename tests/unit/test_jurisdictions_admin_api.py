"""DIAAT-356, DIAAT-376: jurisdiction administrators add jurisdictions from the admin area, so nobody
runs anything on a cluster. A JurisdictionSuperAdministrator manages (and creates) any jurisdiction;
a JurisdictionAdministrator only the one it works in; nobody else, a SystemAdministrator included.
Real lookups on SQLite. (Who holds a role in a jurisdiction is the assignment endpoints': DIAAT-383,
tests/unit/test_admin_assignments.py; the interim members endpoints are gone.)"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import add_jurisdiction, add_member, add_user, client_as
from transcribe_api.domain import jurisdictions
from transcribe_api.domain.assignments import add_assignment
from transcribe_api.domain.models_dictation import AuditLog, Jurisdiction

pytestmark = pytest.mark.real_jurisdictions

ET_SETTINGS = json.loads(
    (Path(__file__).resolve().parents[2] / "docs" / "jurisdictions" / "et.example.json").read_text()
)


@pytest.fixture
def admin_app(jurisdiction_db, monkeypatch):
    from transcribe_api.api import routes_dictation_privileged

    monkeypatch.setattr(routes_dictation_privileged, "get_engine", lambda: jurisdiction_db)
    app = FastAPI()
    app.include_router(routes_dictation_privileged.router, prefix="/api")
    return app


@pytest.fixture
def as_admin(jurisdiction_db, admin_app):
    """A JurisdictionSuperAdministrator (global), with an IAC to manage."""
    admin = add_user(jurisdiction_db, "admin@example.test", roles=("JurisdictionSuperAdministrator",))
    add_jurisdiction(jurisdiction_db, "IAC")
    with Session(jurisdiction_db) as session:
        add_assignment(session, role="JurisdictionSuperAdministrator", jurisdiction_id=None, user_id=admin.id)
        session.commit()
    return client_as(admin_app, jurisdiction_db, admin)


def _audit(engine) -> list[tuple[str, str]]:
    with Session(engine) as session:
        return [(r.event_type, r.outcome) for r in session.exec(select(AuditLog)).all()]


def test_an_administrator_adds_the_et_and_counts_its_members(jurisdiction_db, as_admin):
    created = as_admin.put("/api/admin/jurisdictions/et", json={"name": "Employment Tribunal", "settings": ET_SETTINGS})
    assert created.status_code == 200
    assert created.json()["code"] == "ET"
    assert created.json()["enabled"] is True
    assert "auto_enrol" not in created.json()  # DIAAT-373: nobody is enrolled automatically

    with Session(jurisdiction_db) as session:
        et = session.exec(select(Jurisdiction).where(Jurisdiction.code == "ET")).one()
        for email in ("saad.shaukat@hmcts.net", "hamza.ahmed@justice.gov.uk", "christopher.john@justice.gov.uk"):
            add_assignment(session, role="Judge", jurisdiction_id=et.id, email=email)
        session.commit()
    listed = {j["code"]: j["member_count"] for j in as_admin.get("/api/admin/jurisdictions").json()}
    assert listed == {"ET": 3, "IAC": 0}
    assert ("jurisdiction.edit", "success") in _audit(jurisdiction_db)


def test_an_administrator_edits_a_jurisdiction(jurisdiction_db, as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json={"name": "Employment Tribunal", "settings": {}})
    edited = as_admin.put(
        "/api/admin/jurisdictions/ET", json={"name": "Employment Tribunals", "settings": {}, "enabled": False}
    ).json()
    assert (edited["name"], edited["enabled"]) == ("Employment Tribunals", False)


def test_mistakes_are_refused_clearly(as_admin):
    bad_settings = as_admin.put(
        "/api/admin/jurisdictions/ET", json={"name": "ET", "settings": {"wording": {"no.such.key": "x"}}}
    )
    assert bad_settings.status_code == 400
    assert "unknown wording keys" in bad_settings.json()["detail"]


def test_the_interim_members_endpoints_are_gone(as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json={"name": "ET", "settings": {}})
    assert as_admin.get("/api/admin/jurisdictions/ET/members").status_code == 404
    assert as_admin.post("/api/admin/jurisdictions/ET/members", json={"email": "a@example.test"}).status_code == 404
    assert as_admin.delete("/api/admin/jurisdictions/ET/members", params={"email": "a@example.test"}).status_code == 404
    # The old-shape user list too (the new one is GET /api/admin/users, DIAAT-383: items with assignments).
    assert as_admin.get("/api/admin/users/00000000-0000-0000-0000-000000000001").status_code == 404


def test_the_wording_keys_are_listed_for_the_form(as_admin):
    keys = as_admin.get("/api/admin/jurisdictions/wording-keys").json()["keys"]
    assert keys == jurisdictions.WORDING_KEYS


@pytest.mark.parametrize("roles", [("Judge",), ("LegalTextManager",)])
def test_only_jurisdiction_administrators_may_manage_jurisdictions(jurisdiction_db, admin_app, roles):
    other = add_user(jurisdiction_db, roles=roles)
    client = client_as(admin_app, jurisdiction_db, other)
    assert client.get("/api/admin/jurisdictions").status_code == 403
    assert client.put("/api/admin/jurisdictions/ET", json={"name": "ET", "settings": {}}).status_code == 403
    assert client.get("/api/admin/jurisdictions/IAC/wording").status_code == 403


def test_a_system_administrator_manages_no_jurisdiction(jurisdiction_db, admin_app):
    """DIAAT-389: the Entra role gives system-level work only, wherever (and whether) it acts."""
    add_jurisdiction(jurisdiction_db, "IAC")
    for roles, ctx in ((("SystemAdministrator",), None), (("Judge", "SystemAdministrator"), ("IAC", "Judge"))):
        sysadmin = add_user(jurisdiction_db, roles=roles)
        client = client_as(admin_app, jurisdiction_db, sysadmin, ctx=ctx, sysadmin=True)
        assert client.get("/api/admin/jurisdictions").status_code == 403
        assert client.put("/api/admin/jurisdictions/ET", json={"name": "ET", "settings": {}}).status_code == 403
        assert client.get("/api/admin/jurisdictions/IAC/wording").status_code == 403
        assert client.put("/api/admin/jurisdictions/IAC/wording", json={"wording": {}}).status_code == 403


@pytest.fixture
def as_iac_admin(jurisdiction_db, admin_app):
    """A JurisdictionAdministrator of the IAC, in a world with an ET too."""
    iac = add_jurisdiction(jurisdiction_db, "IAC")
    et = add_jurisdiction(jurisdiction_db, "ET")
    admin = add_user(jurisdiction_db, "iac.admin@example.test")
    add_member(jurisdiction_db, admin, iac, role="admin")
    add_member(jurisdiction_db, add_user(jurisdiction_db), et)
    return client_as(admin_app, jurisdiction_db, admin)


def test_a_jurisdiction_administrator_sees_and_manages_only_the_jurisdiction_it_works_in(as_iac_admin):
    assert [j["code"] for j in as_iac_admin.get("/api/admin/jurisdictions").json()] == ["IAC"]
    changed = as_iac_admin.put("/api/admin/jurisdictions/IAC", json={"name": "Immigration", "settings": {}})
    assert changed.status_code == 200
    refused = as_iac_admin.put("/api/admin/jurisdictions/ET", json={"name": "ET", "settings": {}})
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "jurisdiction_not_in_scope"


def test_a_jurisdiction_administrator_cannot_create_a_jurisdiction(as_iac_admin):
    created = as_iac_admin.put("/api/admin/jurisdictions/SSCS", json={"name": "SSCS", "settings": {}})
    assert (created.status_code, created.json()["detail"]["code"]) == (403, "permission_denied")


def test_a_super_administrator_creates_jurisdictions(as_admin):
    assert as_admin.put("/api/admin/jurisdictions/SSCS", json={"name": "SSCS", "settings": {}}).status_code == 200


def test_a_save_from_a_stale_form_does_not_undo_a_change_made_meanwhile(jurisdiction_db, as_admin):
    opened = as_admin.put("/api/admin/jurisdictions/ET", json={"name": "ET", "settings": {}}).json()
    assert opened["version"]
    changed = as_admin.put(
        "/api/admin/jurisdictions/ET",
        json={"name": "ET", "settings": ET_SETTINGS, "version": opened["version"]},
    )
    assert changed.status_code == 200

    stale = as_admin.put(
        "/api/admin/jurisdictions/ET", json={"name": "ET", "settings": {}, "version": opened["version"]}
    )
    assert stale.status_code == 409
    assert "changed by someone else" in stale.json()["detail"]
    [et] = [j for j in as_admin.get("/api/admin/jurisdictions").json() if j["code"] == "ET"]
    assert et["settings"]["wording"] == ET_SETTINGS["wording"]


def test_adding_a_jurisdiction_never_overwrites_an_existing_one(jurisdiction_db, as_admin):
    refused = as_admin.put(
        "/api/admin/jurisdictions/iac", json={"name": "Mistake", "settings": {}, "enabled": True, "version": ""}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "IAC already exists"
    [iac] = as_admin.get("/api/admin/jurisdictions").json()
    assert iac["name"] == "IAC name"


def test_a_blank_name_is_refused(as_admin):
    assert as_admin.put("/api/admin/jurisdictions/ET", json={"name": "   ", "settings": {}}).status_code == 422
