"""DIAAT-356: SystemAdministrators add jurisdictions and their members from the admin
area, so nobody runs anything on a cluster. Real lookups on SQLite."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import add_jurisdiction, add_member, add_user, client_as, sign_up
from transcribe_api.domain import jurisdictions
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
    admin = add_user(jurisdiction_db, "admin@example.test", roles=("SystemAdministrator",))
    add_member(jurisdiction_db, admin, add_jurisdiction(jurisdiction_db, "IAC", auto_enrol=True), role="admin")
    return client_as(admin_app, jurisdiction_db, admin)


def _audit(engine) -> list[tuple[str, str]]:
    with Session(engine) as session:
        return [(r.event_type, r.outcome) for r in session.exec(select(AuditLog)).all()]


def test_an_administrator_adds_the_et_and_its_test_judges(jurisdiction_db, as_admin):
    created = as_admin.put("/api/admin/jurisdictions/et", json={"name": "Employment Tribunal", "settings": ET_SETTINGS})
    assert created.status_code == 200
    assert created.json()["code"] == "ET"
    assert (created.json()["enabled"], created.json()["auto_enrol"]) == (True, False)

    for email in ("saad.shaukat@hmcts.net", "Hamza.Ahmed@justice.gov.uk", "christopher.john@Justice.gov.uk"):
        added = as_admin.post("/api/admin/jurisdictions/ET/members", json={"email": email, "role": "judge"})
        assert added.status_code == 201
        assert added.json() == {"email": email.lower(), "role": "judge", "tester": False, "pending": True}

    members = as_admin.get("/api/admin/jurisdictions/ET/members").json()
    assert [m["email"] for m in members] == [
        "christopher.john@justice.gov.uk",
        "hamza.ahmed@justice.gov.uk",
        "saad.shaukat@hmcts.net",
    ]
    listed = {j["code"]: j["member_count"] for j in as_admin.get("/api/admin/jurisdictions").json()}
    assert listed == {"ET": 3, "IAC": 1}
    assert _audit(jurisdiction_db).count(("membership.add", "success")) == 3
    assert ("jurisdiction.edit", "success") in _audit(jurisdiction_db)


def test_a_test_judge_added_in_advance_works_in_the_et_only(jurisdiction_db, as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json={"name": "Employment Tribunal", "settings": ET_SETTINGS})
    as_admin.post("/api/admin/jurisdictions/ET/members", json={"email": "hamza.ahmed@justice.gov.uk", "tester": True})
    as_admin.post("/api/admin/jurisdictions/ET/members", json={"email": "et.admin@example.test", "role": "admin"})
    as_admin.put(
        "/api/admin/jurisdictions/ET", json={"name": "Employment Tribunal", "settings": ET_SETTINGS, "stage": "test"}
    )

    judge = sign_up(jurisdiction_db, "Hamza.Ahmed@justice.gov.uk")

    assert [m.code for m in jurisdictions.load_memberships(judge.id)] == ["ET"]
    [member] = [m for m in as_admin.get("/api/admin/jurisdictions/ET/members").json() if m["email"] == "hamza.ahmed@justice.gov.uk"]
    assert (member["pending"], member["tester"]) == (False, True)


def test_an_administrator_removes_a_member_and_edits_a_jurisdiction(jurisdiction_db, as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json={"name": "Employment Tribunal", "settings": {}})
    as_admin.post("/api/admin/jurisdictions/ET/members", json={"email": "a@example.test"})

    assert as_admin.delete("/api/admin/jurisdictions/ET/members", params={"email": "a@example.test"}).status_code == 204
    assert as_admin.get("/api/admin/jurisdictions/ET/members").json() == []
    edited = as_admin.put(
        "/api/admin/jurisdictions/ET", json={"name": "Employment Tribunals", "settings": {}, "enabled": False}
    ).json()
    assert (edited["name"], edited["enabled"]) == ("Employment Tribunals", False)
    assert ("membership.remove", "success") in _audit(jurisdiction_db)


def test_mistakes_are_refused_clearly(as_admin):
    bad_settings = as_admin.put(
        "/api/admin/jurisdictions/ET", json={"name": "ET", "settings": {"wording": {"no.such.key": "x"}}}
    )
    assert bad_settings.status_code == 400
    assert "unknown wording keys" in bad_settings.json()["detail"]
    assert as_admin.post("/api/admin/jurisdictions/NOPE/members", json={"email": "a@example.test"}).status_code == 404
    as_admin.put("/api/admin/jurisdictions/ET", json={"name": "ET", "settings": {}})
    assert as_admin.post("/api/admin/jurisdictions/ET/members", json={"email": "not-an-email"}).status_code == 422
    assert (
        as_admin.post("/api/admin/jurisdictions/ET/members", json={"email": "a@example.test", "role": "clerk"}).status_code
        == 400
    )
    assert as_admin.delete("/api/admin/jurisdictions/ET/members", params={"email": "x@example.test"}).status_code == 404


def test_the_wording_keys_are_listed_for_the_form(as_admin):
    keys = as_admin.get("/api/admin/jurisdictions/wording-keys").json()["keys"]
    assert keys == jurisdictions.WORDING_KEYS


@pytest.mark.parametrize("roles", [("Judge",), ("LegalTextManager",)])
def test_only_a_system_administrator_may_manage_jurisdictions(jurisdiction_db, admin_app, roles):
    other = add_user(jurisdiction_db, roles=roles)
    client = client_as(admin_app, jurisdiction_db, other)
    assert client.get("/api/admin/jurisdictions").status_code == 403
    assert client.put("/api/admin/jurisdictions/ET", json={"name": "ET", "settings": {}}).status_code == 403
    assert client.post("/api/admin/jurisdictions/IAC/members", json={"email": "x@example.test"}).status_code == 403


def _audit_details(engine, action: str) -> list[dict]:
    with Session(engine) as session:
        return [r.detail for r in session.exec(select(AuditLog).where(AuditLog.event_type == action)).all()]


def test_the_audit_says_who_was_added_or_removed_without_contact_details(jurisdiction_db, as_admin):
    """By user ID once they have signed in, else by a hash of the email (domain/audit.py:
    the audit log stays free of contact details)."""
    as_admin.put("/api/admin/jurisdictions/ET", json={"name": "Employment Tribunal", "settings": ET_SETTINGS})
    as_admin.post("/api/admin/jurisdictions/ET/members", json={"email": "A@example.test", "role": "admin"})
    judge = sign_up(jurisdiction_db, "a@example.test")
    as_admin.delete("/api/admin/jurisdictions/ET/members", params={"email": "a@example.test"})

    [edit] = _audit_details(jurisdiction_db, "jurisdiction.edit")
    assert (edit["code"], edit["created"], edit["name"]) == ("ET", True, "Employment Tribunal")
    assert _audit_details(jurisdiction_db, "membership.add") == [
        {
            "code": "ET",
            "target_email_sha256": hashlib.sha256(b"a@example.test").hexdigest(),
            "role": "admin",
            "tester": False,
            "pending": True,
        }
    ]
    assert _audit_details(jurisdiction_db, "membership.remove") == [
        {"code": "ET", "target_user_ids": [str(judge.id)]}
    ]
    with Session(jurisdiction_db) as session:
        assert "example.test" not in json.dumps([r.detail for r in session.exec(select(AuditLog)).all()])


def test_a_pending_email_stored_in_another_case_is_still_one_member(jurisdiction_db, as_admin):
    """A row put in by hand as Mixed@Case: adding again updates it, sign-in claims it."""
    with Session(jurisdiction_db) as session:
        et = session.exec(select(Jurisdiction).where(Jurisdiction.code == "IAC")).one()
    add_member(jurisdiction_db, None, et, email="Mixed@Example.test")
    as_admin.post("/api/admin/jurisdictions/IAC/members", json={"email": "mixed@example.test", "role": "admin"})

    members = [m for m in as_admin.get("/api/admin/jurisdictions/IAC/members").json() if m["pending"]]
    assert members == [{"email": "mixed@example.test", "role": "admin", "tester": False, "pending": True}]
    judge = sign_up(jurisdiction_db, "mixed@example.test")
    assert [(m.code, m.role) for m in jurisdictions.load_memberships(judge.id)] == [("IAC", "admin")]


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
    assert (iac["name"], iac["auto_enrol"]) == ("IAC name", True)


def test_a_blank_name_is_refused(as_admin):
    assert as_admin.put("/api/admin/jurisdictions/ET", json={"name": "   ", "settings": {}}).status_code == 422


def test_a_judge_made_admin_here_edits_the_wording_and_loses_it_when_removed(jurisdiction_db, as_admin, monkeypatch):
    """Acceptance 2 end to end: the product team makes a signed-in ET judge its admin."""
    from fastapi.testclient import TestClient

    from transcribe_api.api import routes_jurisdictions

    monkeypatch.setattr(routes_jurisdictions, "get_engine", lambda: jurisdiction_db)
    as_admin.put("/api/admin/jurisdictions/ET", json={"name": "Employment Tribunal", "settings": ET_SETTINGS})
    as_admin.post("/api/admin/jurisdictions/ET/members", json={"email": "et.lead@example.test", "role": "admin"})
    as_admin.put(
        "/api/admin/jurisdictions/ET", json={"name": "Employment Tribunal", "settings": ET_SETTINGS, "stage": "live"}
    )
    judge = sign_up(jurisdiction_db, "et.judge@example.test")
    as_admin.post("/api/admin/jurisdictions/ET/members", json={"email": "et.judge@example.test"})

    app = FastAPI()
    app.include_router(routes_jurisdictions.router, prefix="/api")
    judge_client: TestClient = client_as(app, jurisdiction_db, judge)
    headers = {"X-Jurisdiction": "ET"}
    assert judge_client.get("/api/jurisdictions/wording", headers=headers).status_code == 403

    made_admin = as_admin.post(
        "/api/admin/jurisdictions/ET/members", json={"email": "et.judge@example.test", "role": "admin"}
    )
    assert made_admin.json() == {"email": "et.judge@example.test", "role": "admin", "tester": False, "pending": False}
    saved = judge_client.put("/api/jurisdictions/wording", headers=headers, json={"wording": {"home.intro": "Hello"}})
    assert saved.status_code == 200
    # The IAC, where they were enrolled as a judge on sign-up, stays out of reach.
    assert judge_client.get("/api/jurisdictions/wording", headers={"X-Jurisdiction": "IAC"}).status_code == 403

    as_admin.delete("/api/admin/jurisdictions/ET/members", params={"email": "et.judge@example.test"})
    assert judge_client.get("/api/jurisdictions/wording", headers=headers).status_code == 403
