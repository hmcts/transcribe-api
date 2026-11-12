"""DIAAT-356, DIAAT-406: a jurisdiction's screen wording. The JurisdictionSuperAdministrator edits it from the
Jurisdictions screen (``/api/admin/jurisdictions/{code}/wording``); ``/api/jurisdictions/wording``, the
wording of the jurisdiction the caller acts in, is refused to everyone for now, a JurisdictionAdministrator
included. Real lookups on SQLite."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import add_jurisdiction, add_member, add_user, client_as
from transcribe_api.domain import jurisdictions
from transcribe_api.domain.assignments import add_assignment
from transcribe_api.domain.models_dictation import AuditLog, Jurisdiction

pytestmark = pytest.mark.real_jurisdictions

IAC_SETTINGS = {
    "wording": {"help.subtitle": "Immigration and Asylum Chamber"},
    "template": "iac_decision",
    "features": {"hearing_recordings": True},
}


ET_WORDING = "/api/admin/jurisdictions/ET/wording"


@pytest.fixture
def setup(jurisdiction_db, jurisdiction_app, monkeypatch):
    """An IAC judge who is also an ET admin, and an ET judge."""
    from transcribe_api.api import routes_dictation_privileged, routes_jurisdictions

    monkeypatch.setattr(routes_jurisdictions, "get_engine", lambda: jurisdiction_db)
    monkeypatch.setattr(routes_dictation_privileged, "get_engine", lambda: jurisdiction_db)
    jurisdiction_app.include_router(routes_dictation_privileged.router, prefix="/api")
    iac = add_jurisdiction(jurisdiction_db, "IAC", settings=IAC_SETTINGS)
    et = add_jurisdiction(jurisdiction_db, "ET", settings={"wording": {"help.subtitle": "Employment Tribunal"}})
    et_admin = add_user(jurisdiction_db)
    add_member(jurisdiction_db, et_admin, iac)
    add_member(jurisdiction_db, et_admin, et, role="admin")
    et_judge = add_user(jurisdiction_db)
    add_member(jurisdiction_db, et_judge, et)
    return et_admin, et_judge


@pytest.fixture
def as_super(jurisdiction_db, jurisdiction_app, setup):
    """A JurisdictionSuperAdministrator (global)."""
    jsuper = add_user(jurisdiction_db, roles=("JurisdictionSuperAdministrator",))
    with Session(jurisdiction_db) as session:
        add_assignment(session, role="JurisdictionSuperAdministrator", jurisdiction_id=None, user_id=jsuper.id)
        session.commit()
    return client_as(jurisdiction_app, jurisdiction_db, jsuper)


def _settings(engine, code: str) -> dict:
    with Session(engine) as session:
        return session.exec(select(Jurisdiction).where(Jurisdiction.code == code)).one().settings


def test_the_super_administrator_edits_a_jurisdictions_wording(jurisdiction_db, as_super):
    current = as_super.get(ET_WORDING)
    assert current.status_code == 200
    assert current.json()["wording"] == {"help.subtitle": "Employment Tribunal"}
    assert current.json()["keys"] == jurisdictions.WORDING_KEYS

    saved = as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "  Employment Tribunals  ", "home.intro": " "}})
    assert saved.status_code == 200
    # Trimmed; blank text falls back to the default.
    assert saved.json()["wording"] == {"help.subtitle": "Employment Tribunals"}
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Employment Tribunals"}
    with Session(jurisdiction_db) as session:
        [audit] = session.exec(select(AuditLog).where(AuditLog.event_type == "jurisdiction.edit")).all()
    assert audit.outcome == "success"


def test_their_judges_see_the_new_wording(jurisdiction_db, jurisdiction_app, setup, as_super):
    _, et_judge = setup
    assert as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "ET"}}).status_code == 200
    current = client_as(jurisdiction_app, jurisdiction_db, et_judge).get("/api/jurisdictions/current").json()
    assert current["wording"] == {"help.subtitle": "ET"}


@pytest.mark.parametrize("acting_in", ["ET", "IAC"])
def test_a_jurisdiction_administrator_changes_no_wording_where_it_acts(
    jurisdiction_db, jurisdiction_app, setup, acting_in
):
    et_admin, _ = setup
    client = client_as(jurisdiction_app, jurisdiction_db, et_admin)
    headers = {"X-Jurisdiction": acting_in}

    for request in (
        lambda: client.get("/api/jurisdictions/wording", headers=headers),
        lambda: client.put("/api/jurisdictions/wording", headers=headers, json={"wording": {"help.subtitle": "x"}}),
        lambda: client.get(f"/api/admin/jurisdictions/{acting_in}/wording", headers=headers),
        lambda: client.put(
            f"/api/admin/jurisdictions/{acting_in}/wording", headers=headers, json={"wording": {"help.subtitle": "x"}}
        ),
    ):
        refused = request()
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "permission_denied"
    assert _settings(jurisdiction_db, "IAC") == IAC_SETTINGS
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Employment Tribunal"}


def test_the_super_administrator_has_no_jurisdiction_to_edit_in_place(jurisdiction_db, as_super):
    for refused in (
        as_super.get("/api/jurisdictions/wording"),
        as_super.put("/api/jurisdictions/wording", json={"wording": {"help.subtitle": "x"}}),
    ):
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "no_jurisdiction"
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Employment Tribunal"}


def test_a_jurisdiction_they_do_not_belong_to_is_out_of_reach(jurisdiction_db, jurisdiction_app, setup):
    """A tab naming a jurisdiction the user's token is not in is out of step with it (409)."""
    _, et_judge = setup
    add_jurisdiction(jurisdiction_db, "SSCS")
    refused = client_as(jurisdiction_app, jurisdiction_db, et_judge).put(
        "/api/jurisdictions/wording", headers={"X-Jurisdiction": "IAC"}, json={"wording": {"help.subtitle": "x"}}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "context_changed"
    assert _settings(jurisdiction_db, "IAC") == IAC_SETTINGS


def test_a_judge_cannot_change_their_own_jurisdictions_wording(jurisdiction_db, jurisdiction_app, setup):
    _, et_judge = setup
    client = client_as(jurisdiction_app, jurisdiction_db, et_judge)
    assert client.get("/api/jurisdictions/wording").status_code == 403
    assert client.put("/api/jurisdictions/wording", json={"wording": {}}).status_code == 403


def test_the_wording_change_keeps_the_rest_of_the_settings(jurisdiction_db, as_super):
    assert (
        as_super.put("/api/admin/jurisdictions/IAC/wording", json={"wording": {"help.subtitle": "IAC"}}).status_code
        == 200
    )
    assert _settings(jurisdiction_db, "IAC") == {**IAC_SETTINGS, "wording": {"help.subtitle": "IAC"}}


def test_unknown_or_overlong_wording_is_refused(jurisdiction_db, as_super):
    unknown = as_super.put(ET_WORDING, json={"wording": {"no.such.key": "x"}})
    assert unknown.status_code == 400
    assert "unknown wording keys" in str(unknown.json()["detail"])
    long = as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "x" * 501}})
    assert long.status_code == 400
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Employment Tribunal"}


def test_the_admin_of_a_disabled_jurisdiction_cannot_edit_it_either(jurisdiction_db, jurisdiction_app, setup):
    et_admin, _ = setup
    with Session(jurisdiction_db) as session:
        et = session.exec(select(Jurisdiction).where(Jurisdiction.code == "ET")).one()
        et.enabled = False
        session.add(et)
        session.commit()
    # Their token, minted before it was disabled, still says they act as its admin.
    refused = client_as(jurisdiction_app, jurisdiction_db, et_admin, ctx=("ET", "JurisdictionAdministrator")).put(
        "/api/jurisdictions/wording", headers={"X-Jurisdiction": "ET"}, json={"wording": {"help.subtitle": "x"}}
    )
    assert refused.status_code == 403
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Employment Tribunal"}


def test_settings_this_release_does_not_know_are_kept(jurisdiction_db, as_super):
    """A newer release's template, during a rolling deploy, survives a wording save."""
    with Session(jurisdiction_db) as session:
        et = session.exec(select(Jurisdiction).where(Jurisdiction.code == "ET")).one()
        et.settings = {"wording": {}, "template": "et_judgment", "features": {"future_thing": True}}
        session.add(et)
        session.commit()
    assert as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "ET"}}).status_code == 200
    assert _settings(jurisdiction_db, "ET") == {
        "wording": {"help.subtitle": "ET"},
        "template": "et_judgment",
        "features": {"future_thing": True},
    }


def test_a_blank_unknown_key_is_refused_too(as_super):
    assert as_super.put(ET_WORDING, json={"wording": {"no.such.key": " "}}).status_code == 400


def test_the_audit_names_the_wording_that_changed(jurisdiction_db, as_super):
    as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "Employment Tribunal", "home.intro": "Hello"}})
    with Session(jurisdiction_db) as session:
        [audit] = session.exec(select(AuditLog).where(AuditLog.event_type == "jurisdiction.edit")).all()
    assert audit.detail == {"code": "ET", "fields": ["wording"], "wording_keys_changed": ["home.intro"]}


def test_a_stale_wording_form_does_not_undo_a_change_made_meanwhile(jurisdiction_db, as_super):
    opened = as_super.get(ET_WORDING).json()
    first = as_super.put(
        ET_WORDING,
        json={"wording": {"help.subtitle": "Theirs"}, "version": opened["version"]},
    )
    assert first.status_code == 200
    assert first.json()["version"] != opened["version"]

    stale = as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "Mine"}, "version": opened["version"]})
    assert stale.status_code == 409
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Theirs"}
    again = as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "Mine"}, "version": first.json()["version"]})
    assert again.status_code == 200


def test_the_wording_shown_is_the_version_it_names(jurisdiction_db, as_super, monkeypatch):
    """The wording shown and its version come from one read: an older wording with the
    newer version would let a save undo a change saved meanwhile."""
    from transcribe_api import jurisdictions_admin

    real = jurisdictions_admin.wording_and_version

    def changed_meanwhile(session, code):
        with Session(jurisdiction_db) as other:
            jurisdictions_admin.set_wording(other, code, {"help.subtitle": "Theirs"})
        return real(session, code)

    monkeypatch.setattr(jurisdictions_admin, "wording_and_version", changed_meanwhile)
    shown = as_super.get(ET_WORDING).json()
    monkeypatch.setattr(jurisdictions_admin, "wording_and_version", real)

    assert shown["wording"] == {"help.subtitle": "Employment Tribunal"}
    saved = as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "Mine"}, "version": shown["version"]})
    assert saved.status_code == 409
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Theirs"}


def test_a_save_answers_with_its_own_version_not_one_saved_just_after(jurisdiction_db, as_super, monkeypatch):
    """Another admin's save landing right after this one commits must not lend this
    response its version: the editor would then hold this wording with that version,
    and its next save would undo the other change instead of being refused (409)."""
    from transcribe_api import jurisdictions_admin

    opened = as_super.get(ET_WORDING).json()
    real = jurisdictions_admin.set_wording

    def then_another_admin_saves(session, code, wording, **kwargs):
        saved = real(session, code, wording, **kwargs)
        with Session(jurisdiction_db) as other:
            real(other, code, {"help.subtitle": "Theirs"})
        return saved

    monkeypatch.setattr(jurisdictions_admin, "set_wording", then_another_admin_saves)
    mine = as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "Mine"}, "version": opened["version"]}).json()
    monkeypatch.setattr(jurisdictions_admin, "set_wording", real)

    assert mine["wording"] == {"help.subtitle": "Mine"}
    again = as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "Mine, again"}, "version": mine["version"]})
    assert again.status_code == 409
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Theirs"}


def test_the_audit_compares_with_the_wording_the_save_replaced(jurisdiction_db, as_super, monkeypatch):
    """Not the wording read when the request began: a change saved in between counts."""
    from transcribe_api import jurisdictions_admin

    real = jurisdictions_admin.set_wording

    def another_admin_saves_first(session, code, wording, **kwargs):
        with Session(jurisdiction_db) as other:
            real(other, code, {"help.subtitle": "Employment Tribunal", "home.intro": "Hi"})
        return real(session, code, wording, **kwargs)

    monkeypatch.setattr(jurisdictions_admin, "set_wording", another_admin_saves_first)
    as_super.put(ET_WORDING, json={"wording": {"help.subtitle": "Employment Tribunal"}})
    with Session(jurisdiction_db) as session:
        [audit] = session.exec(select(AuditLog).where(AuditLog.event_type == "jurisdiction.edit")).all()
    # This save removed home.intro; the request began before it was added.
    assert audit.detail["wording_keys_changed"] == ["home.intro"]
