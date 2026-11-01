"""DIAAT-356: a judge made an admin for a jurisdiction edits that jurisdiction's
wording, and nothing from any other. Real lookups on SQLite."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import add_jurisdiction, add_member, add_user, client_as
from transcribe_api.domain import jurisdictions
from transcribe_api.domain.models_dictation import AuditLog, Jurisdiction

pytestmark = pytest.mark.real_jurisdictions

IAC_SETTINGS = {
    "wording": {"help.subtitle": "Immigration and Asylum Chamber"},
    "template": "iac_decision",
    "features": {"hearing_recordings": True},
}


@pytest.fixture
def setup(jurisdiction_db, jurisdiction_app, monkeypatch):
    """An IAC judge who is also an ET admin, and an ET judge."""
    from transcribe_api.api import routes_jurisdictions

    monkeypatch.setattr(routes_jurisdictions, "get_engine", lambda: jurisdiction_db)
    iac = add_jurisdiction(jurisdiction_db, "IAC", settings=IAC_SETTINGS)
    et = add_jurisdiction(jurisdiction_db, "ET", settings={"wording": {"help.subtitle": "Employment Tribunal"}})
    et_admin = add_user(jurisdiction_db)
    add_member(jurisdiction_db, et_admin, iac)
    add_member(jurisdiction_db, et_admin, et, role="admin")
    et_judge = add_user(jurisdiction_db)
    add_member(jurisdiction_db, et_judge, et)
    return et_admin, et_judge


def _settings(engine, code: str) -> dict:
    with Session(engine) as session:
        return session.exec(select(Jurisdiction).where(Jurisdiction.code == code)).one().settings


def test_an_admin_edits_their_jurisdictions_wording(jurisdiction_db, jurisdiction_app, setup):
    et_admin, _ = setup
    client = client_as(jurisdiction_app, jurisdiction_db, et_admin)

    current = client.get("/api/jurisdictions/wording", headers={"X-Jurisdiction": "ET"})
    assert current.status_code == 200
    assert current.json()["wording"] == {"help.subtitle": "Employment Tribunal"}
    assert current.json()["keys"] == jurisdictions.WORDING_KEYS

    saved = client.put(
        "/api/jurisdictions/wording",
        headers={"X-Jurisdiction": "ET"},
        json={"wording": {"help.subtitle": "  Employment Tribunals  ", "home.intro": " "}},
    )
    assert saved.status_code == 200
    # Trimmed; blank text falls back to the default.
    assert saved.json()["wording"] == {"help.subtitle": "Employment Tribunals"}
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Employment Tribunals"}
    with Session(jurisdiction_db) as session:
        [audit] = session.exec(select(AuditLog).where(AuditLog.event_type == "jurisdiction.edit")).all()
    assert audit.outcome == "success"


def test_their_judges_see_the_new_wording(jurisdiction_db, jurisdiction_app, setup):
    et_admin, et_judge = setup
    client_as(jurisdiction_app, jurisdiction_db, et_admin).put(
        "/api/jurisdictions/wording", headers={"X-Jurisdiction": "ET"}, json={"wording": {"help.subtitle": "ET"}}
    )
    me = client_as(jurisdiction_app, jurisdiction_db, et_judge).get("/api/jurisdictions/me").json()
    assert me["current"]["wording"] == {"help.subtitle": "ET"}


def test_an_admin_in_one_jurisdiction_cannot_change_another(jurisdiction_db, jurisdiction_app, setup):
    et_admin, _ = setup
    client = client_as(jurisdiction_app, jurisdiction_db, et_admin)

    # A judge in the IAC, not its admin.
    for request in (
        lambda: client.get("/api/jurisdictions/wording", headers={"X-Jurisdiction": "IAC"}),
        lambda: client.put(
            "/api/jurisdictions/wording", headers={"X-Jurisdiction": "IAC"}, json={"wording": {"help.subtitle": "x"}}
        ),
    ):
        refused = request()
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "jurisdiction_admin_required"
    assert _settings(jurisdiction_db, "IAC") == IAC_SETTINGS


def test_a_jurisdiction_they_do_not_belong_to_is_out_of_reach(jurisdiction_db, jurisdiction_app, setup):
    _, et_judge = setup
    add_jurisdiction(jurisdiction_db, "SSCS")
    refused = client_as(jurisdiction_app, jurisdiction_db, et_judge).put(
        "/api/jurisdictions/wording", headers={"X-Jurisdiction": "IAC"}, json={"wording": {"help.subtitle": "x"}}
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == jurisdictions.NOT_AVAILABLE
    assert _settings(jurisdiction_db, "IAC") == IAC_SETTINGS


def test_a_judge_cannot_change_their_own_jurisdictions_wording(jurisdiction_db, jurisdiction_app, setup):
    _, et_judge = setup
    client = client_as(jurisdiction_app, jurisdiction_db, et_judge)
    assert client.get("/api/jurisdictions/wording").status_code == 403
    assert client.put("/api/jurisdictions/wording", json={"wording": {}}).status_code == 403


def test_the_wording_change_keeps_the_rest_of_the_settings(jurisdiction_db, jurisdiction_app, setup):
    with Session(jurisdiction_db) as session:
        iac = session.exec(select(Jurisdiction).where(Jurisdiction.code == "IAC")).one()
    iac_admin = add_user(jurisdiction_db)
    add_member(jurisdiction_db, iac_admin, iac, role="admin")
    client = client_as(jurisdiction_app, jurisdiction_db, iac_admin)

    assert client.put("/api/jurisdictions/wording", json={"wording": {"help.subtitle": "IAC"}}).status_code == 200
    assert _settings(jurisdiction_db, "IAC") == {**IAC_SETTINGS, "wording": {"help.subtitle": "IAC"}}


def test_unknown_or_overlong_wording_is_refused(jurisdiction_db, jurisdiction_app, setup):
    et_admin, _ = setup
    client = client_as(jurisdiction_app, jurisdiction_db, et_admin)
    headers = {"X-Jurisdiction": "ET"}
    unknown = client.put("/api/jurisdictions/wording", headers=headers, json={"wording": {"no.such.key": "x"}})
    assert unknown.status_code == 400
    assert "unknown wording keys" in unknown.json()["detail"]
    long = client.put("/api/jurisdictions/wording", headers=headers, json={"wording": {"help.subtitle": "x" * 501}})
    assert long.status_code == 400
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Employment Tribunal"}


def test_the_admin_of_a_disabled_jurisdiction_cannot_edit_it(jurisdiction_db, jurisdiction_app, setup):
    et_admin, _ = setup
    with Session(jurisdiction_db) as session:
        et = session.exec(select(Jurisdiction).where(Jurisdiction.code == "ET")).one()
        et.enabled = False
        session.add(et)
        session.commit()
    refused = client_as(jurisdiction_app, jurisdiction_db, et_admin).put(
        "/api/jurisdictions/wording", headers={"X-Jurisdiction": "ET"}, json={"wording": {"help.subtitle": "x"}}
    )
    assert refused.status_code == 403
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Employment Tribunal"}


def test_settings_this_release_does_not_know_are_kept(jurisdiction_db, jurisdiction_app, setup):
    """A newer release's template, during a rolling deploy, survives a wording save."""
    et_admin, _ = setup
    with Session(jurisdiction_db) as session:
        et = session.exec(select(Jurisdiction).where(Jurisdiction.code == "ET")).one()
        et.settings = {"wording": {}, "template": "et_judgment", "features": {"future_thing": True}}
        session.add(et)
        session.commit()
    client_as(jurisdiction_app, jurisdiction_db, et_admin).put(
        "/api/jurisdictions/wording", headers={"X-Jurisdiction": "ET"}, json={"wording": {"help.subtitle": "ET"}}
    )
    assert _settings(jurisdiction_db, "ET") == {
        "wording": {"help.subtitle": "ET"},
        "template": "et_judgment",
        "features": {"future_thing": True},
    }


def test_a_blank_unknown_key_is_refused_too(jurisdiction_db, jurisdiction_app, setup):
    et_admin, _ = setup
    refused = client_as(jurisdiction_app, jurisdiction_db, et_admin).put(
        "/api/jurisdictions/wording", headers={"X-Jurisdiction": "ET"}, json={"wording": {"no.such.key": " "}}
    )
    assert refused.status_code == 400


def test_the_audit_names_the_wording_that_changed(jurisdiction_db, jurisdiction_app, setup):
    et_admin, _ = setup
    client_as(jurisdiction_app, jurisdiction_db, et_admin).put(
        "/api/jurisdictions/wording",
        headers={"X-Jurisdiction": "ET"},
        json={"wording": {"help.subtitle": "Employment Tribunal", "home.intro": "Hello"}},
    )
    with Session(jurisdiction_db) as session:
        [audit] = session.exec(select(AuditLog).where(AuditLog.event_type == "jurisdiction.edit")).all()
    assert audit.detail == {"code": "ET", "fields": ["wording"], "wording_keys_changed": ["home.intro"]}


def test_a_stale_wording_form_does_not_undo_a_change_made_meanwhile(jurisdiction_db, jurisdiction_app, setup):
    et_admin, _ = setup
    client = client_as(jurisdiction_app, jurisdiction_db, et_admin)
    headers = {"X-Jurisdiction": "ET"}
    opened = client.get("/api/jurisdictions/wording", headers=headers).json()
    first = client.put(
        "/api/jurisdictions/wording",
        headers=headers,
        json={"wording": {"help.subtitle": "Theirs"}, "version": opened["version"]},
    )
    assert first.status_code == 200
    assert first.json()["version"] != opened["version"]

    stale = client.put(
        "/api/jurisdictions/wording",
        headers=headers,
        json={"wording": {"help.subtitle": "Mine"}, "version": opened["version"]},
    )
    assert stale.status_code == 409
    assert _settings(jurisdiction_db, "ET")["wording"] == {"help.subtitle": "Theirs"}
    again = client.put(
        "/api/jurisdictions/wording",
        headers=headers,
        json={"wording": {"help.subtitle": "Mine"}, "version": first.json()["version"]},
    )
    assert again.status_code == 200


def test_the_wording_shown_is_the_version_it_names(jurisdiction_db, jurisdiction_app, setup, monkeypatch):
    """A change saved after the request's jurisdiction was read is the one shown, with
    its version: an older wording with the newer version would let a save undo it."""
    from transcribe_api import jurisdictions_admin

    et_admin, _ = setup
    client = client_as(jurisdiction_app, jurisdiction_db, et_admin)
    real = jurisdictions_admin.wording_and_version

    def changed_meanwhile(session, code):
        with Session(jurisdiction_db) as other:
            jurisdictions_admin.set_wording(other, code, {"help.subtitle": "Theirs"})
        return real(session, code)

    monkeypatch.setattr(jurisdictions_admin, "wording_and_version", changed_meanwhile)
    shown = client.get("/api/jurisdictions/wording", headers={"X-Jurisdiction": "ET"}).json()
    monkeypatch.setattr(jurisdictions_admin, "wording_and_version", real)

    assert shown["wording"] == {"help.subtitle": "Theirs"}
    saved = client.put(
        "/api/jurisdictions/wording",
        headers={"X-Jurisdiction": "ET"},
        json={"wording": {"help.subtitle": "Mine"}, "version": shown["version"]},
    )
    assert saved.status_code == 200
