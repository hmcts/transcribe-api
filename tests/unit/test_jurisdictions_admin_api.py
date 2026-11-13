"""DIAAT-356, DIAAT-376: jurisdiction administrators add jurisdictions from the admin area, so nobody
runs anything on a cluster. A JurisdictionSuperAdministrator manages (and creates) any jurisdiction;
nobody else does (DIAAT-406), a JurisdictionAdministrator or a SystemAdministrator included.
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
def test_only_the_super_administrator_may_manage_jurisdictions(jurisdiction_db, admin_app, roles):
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


def _permission_denied(response) -> bool:
    return response.status_code == 403 and response.json()["detail"]["code"] == "permission_denied"


def test_a_jurisdiction_administrator_cannot_see_or_manage_any_jurisdiction(jurisdiction_db, as_iac_admin):
    assert _permission_denied(as_iac_admin.get("/api/admin/jurisdictions"))
    assert _permission_denied(as_iac_admin.get("/api/admin/jurisdictions/wording-keys"))
    for code in ("IAC", "ET"):
        assert _permission_denied(
            as_iac_admin.put(f"/api/admin/jurisdictions/{code}", json={"name": f"{code} name", "settings": {}})
        )
        assert _permission_denied(as_iac_admin.get(f"/api/admin/jurisdictions/{code}/wording"))
        assert _permission_denied(as_iac_admin.put(f"/api/admin/jurisdictions/{code}/wording", json={"wording": {}}))
    with Session(jurisdiction_db) as session:
        row = session.exec(select(Jurisdiction).where(Jurisdiction.code == "IAC")).one()
        assert (row.name, row.enabled, row.settings) == ("IAC name", True, {})


def test_a_jurisdiction_administrator_cannot_rename_or_disable_its_own_jurisdiction(jurisdiction_db, as_iac_admin):
    for change in ({"name": "Renamed by the administrator"}, {"enabled": False}):
        refused = as_iac_admin.put("/api/admin/jurisdictions/IAC", json={"name": "IAC name", "settings": {}} | change)
        assert _permission_denied(refused), change
    with Session(jurisdiction_db) as session:
        row = session.exec(select(Jurisdiction).where(Jurisdiction.code == "IAC")).one()
        assert (row.name, row.enabled) == ("IAC name", True)
    assert [t for t in _audit(jurisdiction_db) if t[0] == "access.denied" or t[1] == "denied"]


def test_a_super_administrator_changes_the_settings_and_settings_are_still_validated(as_admin):
    current = as_admin.get("/api/admin/jurisdictions").json()[0]
    body = {
        "name": current["name"],
        "enabled": True,
        "settings": {"wording": {"no.such.key": "x"}},
        "version": current["version"],
    }
    assert as_admin.put("/api/admin/jurisdictions/IAC", json=body).status_code == 400
    body["settings"] = {}
    assert as_admin.put("/api/admin/jurisdictions/IAC", json=body).status_code == 200


def test_a_super_administrator_renames_and_disables_a_jurisdiction(as_admin):
    current = as_admin.get("/api/admin/jurisdictions").json()[0]
    done = as_admin.put(
        "/api/admin/jurisdictions/IAC",
        json={"name": "Renamed", "settings": {}, "enabled": False, "version": current["version"]},
    )
    assert done.status_code == 200 and (done.json()["name"], done.json()["enabled"]) == ("Renamed", False)


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
    assert stale.json()["detail"]["code"] == "edit_conflict"
    assert "changed by someone else" in stale.json()["detail"]["message"]
    [et] = [j for j in as_admin.get("/api/admin/jurisdictions").json() if j["code"] == "ET"]
    assert et["settings"]["wording"] == ET_SETTINGS["wording"]


def test_adding_a_jurisdiction_never_overwrites_an_existing_one(jurisdiction_db, as_admin):
    refused = as_admin.put(
        "/api/admin/jurisdictions/iac", json={"name": "Mistake", "settings": {}, "enabled": True, "version": ""}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"code": "already_exists", "message": "IAC already exists"}
    [iac] = as_admin.get("/api/admin/jurisdictions").json()
    assert iac["name"] == "IAC name"


def test_a_blank_name_is_refused(as_admin):
    assert as_admin.put("/api/admin/jurisdictions/ET", json={"name": "   ", "settings": {}}).status_code == 422


# DIAAT-348: who changed which setting, when, what it was before; going back -------------


def _put(client, code: str, **body) -> dict:
    current = next(j for j in client.get("/api/admin/jurisdictions").json() if j["code"] == code)
    saved = client.put(
        f"/api/admin/jurisdictions/{code}",
        json={"name": current["name"], "settings": current["settings"], "version": current["version"]} | body,
    )
    assert saved.status_code == 200, saved.json()
    return saved.json()


def _history(client, code: str) -> list[dict]:
    response = client.get(f"/api/admin/jurisdictions/{code}/history")
    assert response.status_code == 200, response.json()
    return response.json()["versions"]


def test_the_history_says_who_changed_a_setting_when_and_what_it_was_before(as_admin):
    _put(as_admin, "IAC", settings={"wording": {"help.subtitle": "Asylum appeals"}})
    current = _put(as_admin, "IAC", settings={"wording": {"help.subtitle": "Immigration and asylum appeals"}})

    newest, before, *_ = _history(as_admin, "IAC")
    assert newest["saved_by"]["email"] == "admin@example.test"
    assert newest["saved_at"]
    assert newest["changes"] == [
        {
            "setting": "settings.wording.help.subtitle",
            "before": "Asylum appeals",
            "after": "Immigration and asylum appeals",
        }
    ]
    assert {"setting": "settings.wording.help.subtitle", "before": None, "after": "Asylum appeals"} in before["changes"]
    assert newest["content_as_now"] is True and before["content_as_now"] is False
    # The wording page's saves are kept too.
    wording = as_admin.get("/api/admin/jurisdictions/IAC/wording").json()
    as_admin.put("/api/admin/jurisdictions/IAC/wording", json={"wording": {}, "version": wording["version"]})
    assert _history(as_admin, "IAC")[0]["changes"] == [
        {"setting": "settings.wording.help.subtitle", "before": "Immigration and asylum appeals", "after": None}
    ]
    assert current["code"] == "IAC"


def test_a_change_made_outside_the_admin_area_is_kept_before_the_next_save(jurisdiction_db, as_admin):
    """The command line, or the previous release while this one rolls out, saves no
    version: the next save records it first, by no one, so what it changed from is true."""
    _put(as_admin, "IAC", name="Immigration and Asylum Chamber")
    with Session(jurisdiction_db) as session:
        row = session.exec(select(Jurisdiction).where(Jurisdiction.code == "IAC")).one()
        row.name = "Changed elsewhere"
        session.add(row)
        session.commit()

    _put(as_admin, "IAC", name="Immigration and Asylum Chamber (IAC)")

    newest, outside, *_ = _history(as_admin, "IAC")
    assert newest["changes"] == [
        {"setting": "name", "before": "Changed elsewhere", "after": "Immigration and Asylum Chamber (IAC)"}
    ]
    assert outside["saved_by"] is None
    assert outside["changes"] == [
        {"setting": "name", "before": "Immigration and Asylum Chamber", "after": "Changed elsewhere"}
    ]
    assert _history(as_admin, "IAC")[-1]["first"] is True


def test_going_back_puts_the_earlier_settings_back_and_keeps_who_sees_it(jurisdiction_db, as_admin):
    good = _put(as_admin, "IAC", settings={"wording": {"help.subtitle": "Asylum appeals"}})
    _put(as_admin, "IAC", settings={"wording": {"help.subtitle": "A mistake"}}, display_name="Wrong")
    broken = _put(as_admin, "IAC", enabled=False)
    number = next(
        v["number"] for v in _history(as_admin, "IAC") if any(c["after"] == "Asylum appeals" for c in v["changes"])
    )

    restored = as_admin.post(
        f"/api/admin/jurisdictions/IAC/history/{number}/restore", json={"version": broken["version"]}
    )

    assert restored.status_code == 200, restored.json()
    body = restored.json()
    assert (body["settings"]["wording"], body["display_name"]) == (good["settings"]["wording"], None)
    assert (body["enabled"], body["stage"]) == (False, broken["stage"])  # who sees it stays as it is
    newest = _history(as_admin, "IAC")[0]
    assert (newest["restored_from"], newest["content_as_now"]) == (number, True)
    assert {c["setting"] for c in newest["changes"]} == {"settings.wording.help.subtitle", "display_name"}
    with Session(jurisdiction_db) as session:
        audits = [r.detail for r in session.exec(select(AuditLog)).all() if r.detail.get("change") == "restored"]
    assert audits == [{"code": "IAC", "change": "restored", "restored_from": number}]


def test_going_back_from_a_stale_page_or_to_no_such_version_is_refused(as_admin):
    opened = _put(as_admin, "IAC", settings={"wording": {"help.subtitle": "One"}})
    _put(as_admin, "IAC", settings={"wording": {"help.subtitle": "Two"}})

    stale = as_admin.post("/api/admin/jurisdictions/IAC/history/1/restore", json={"version": opened["version"]})
    assert (stale.status_code, stale.json()["detail"]["code"]) == (409, "edit_conflict")
    current = next(j for j in as_admin.get("/api/admin/jurisdictions").json() if j["code"] == "IAC")
    missing = as_admin.post("/api/admin/jurisdictions/IAC/history/99/restore", json={"version": current["version"]})
    assert missing.status_code == 404
    assert current["settings"]["wording"] == {"help.subtitle": "Two"}


def test_a_jurisdiction_administrator_cannot_see_or_go_back_in_a_jurisdictions_history(
    jurisdiction_db, admin_app, as_iac_admin
):
    """As for a save, the history is the JurisdictionSuperAdministrator's only (DIAAT-406)."""
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    iac_admin = admin_app.dependency_overrides[get_current_user]
    super_admin = add_user(jurisdiction_db, "super@example.test", roles=("JurisdictionSuperAdministrator",))
    with Session(jurisdiction_db) as session:
        add_assignment(session, role="JurisdictionSuperAdministrator", jurisdiction_id=None, user_id=super_admin.id)
        session.commit()
    as_admin = client_as(admin_app, jurisdiction_db, super_admin)
    first = _put(as_admin, "IAC", settings={"wording": {"help.subtitle": "First"}})
    _put(as_admin, "IAC", settings={"wording": {"help.subtitle": "Second"}})
    admin_app.dependency_overrides[get_current_user] = iac_admin  # signed in as the IAC's administrator again

    for code in ("IAC", "ET"):
        assert _permission_denied(as_iac_admin.get(f"/api/admin/jurisdictions/{code}/history"))
    refused = as_iac_admin.post("/api/admin/jurisdictions/IAC/history/1/restore", json={"version": first["version"]})
    assert _permission_denied(refused)
    with Session(jurisdiction_db) as session:
        row = session.exec(select(Jurisdiction).where(Jurisdiction.code == "IAC")).one()
        assert row.settings["wording"] == {"help.subtitle": "Second"}
