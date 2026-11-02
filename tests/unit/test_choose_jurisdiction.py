"""DIAAT-332: a judge who sits in more than one jurisdiction chooses which to work in.

They choose after signing in and can switch later; the choice is remembered. Each
document belongs to the jurisdiction it was produced in. Real lookups on SQLite.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.unit.jurisdiction_fixtures import (
    add_jurisdiction,
    add_member,
    add_user,
    client_as,
)
from transcribe_api.domain import interface_dictation
from transcribe_api.domain.jurisdictions import remember_choice

pytestmark = pytest.mark.real_jurisdictions


@pytest.fixture
def judge_in_both(jurisdiction_db):
    iac = add_jurisdiction(jurisdiction_db, "IAC", settings={"template": "iac_decision"})
    et = add_jurisdiction(jurisdiction_db, "ET")
    judge = add_user(jurisdiction_db)
    add_member(jurisdiction_db, judge, iac)
    add_member(jurisdiction_db, judge, et)
    return judge, iac, et


def test_a_judge_in_two_jurisdictions_is_asked_to_choose_then_works_in_their_choice(
    jurisdiction_db, judge_in_both, jurisdiction_app
):
    judge, _iac, _et = judge_in_both
    client = client_as(jurisdiction_app, jurisdiction_db, judge)
    assert client.get("/api/jurisdictions/me").json()["status"] == "choose"

    chosen = client.put("/api/jurisdictions/active", json={"code": "et"})

    assert chosen.status_code == 200
    assert chosen.headers["cache-control"] == "no-store"
    assert chosen.json()["status"] == "ready"
    assert chosen.json()["current"]["code"] == "ET"
    # Remembered: the next sign-in (no tab jurisdiction yet) starts in the ET.
    assert client.get("/api/jurisdictions/me").json()["current"]["code"] == "ET"


def test_they_can_switch_later(jurisdiction_db, judge_in_both, jurisdiction_app, monkeypatch):
    judge, _iac, _et = judge_in_both
    client = client_as(jurisdiction_app, jurisdiction_db, judge)
    client.put("/api/jurisdictions/active", json={"code": "ET"})

    monkeypatch.setattr(
        "transcribe_api.api.routes_dictation.get_document_content",
        lambda _jurisdiction_id: {"hearing_types": [], "legal_frameworks": [], "anonymity": {}},
    )
    # The template follows the switch: the IAC's document content is not the ET's...
    assert client.get("/api/document-content").status_code == 404
    # ...and is theirs again once they switch back.
    assert client.put("/api/jurisdictions/active", json={"code": "IAC"}).json()["current"]["code"] == "IAC"
    assert client.get("/api/jurisdictions/me").json()["current"]["code"] == "IAC"
    assert client.get("/api/document-content").status_code == 200


def test_a_jurisdiction_they_do_not_belong_to_cannot_be_chosen(jurisdiction_db, judge_in_both, jurisdiction_app):
    judge, _iac, _et = judge_in_both
    add_jurisdiction(jurisdiction_db, "SSCS")
    client = client_as(jurisdiction_app, jurisdiction_db, judge)
    client.put("/api/jurisdictions/active", json={"code": "IAC"})

    refused = client.put("/api/jurisdictions/active", json={"code": "SSCS"})

    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "jurisdiction_not_available"
    assert client.get("/api/jurisdictions/me").json()["current"]["code"] == "IAC"  # unchanged


def test_a_user_with_no_jurisdiction_cannot_choose_one(jurisdiction_db, jurisdiction_app):
    add_jurisdiction(jurisdiction_db, "IAC")
    response = client_as(jurisdiction_app, jurisdiction_db, add_user(jurisdiction_db)).put(
        "/api/jurisdictions/active", json={"code": "IAC"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "no_jurisdiction"


def test_each_document_belongs_to_the_jurisdiction_it_was_produced_in(jurisdiction_db, judge_in_both, jurisdiction_app):
    """Two tabs, one in each jurisdiction: each one's work goes to its own."""
    judge, iac, et = judge_in_both
    client = client_as(jurisdiction_app, jurisdiction_db, judge)
    client.put("/api/jurisdictions/active", json={"code": "IAC"})  # the last choice: the IAC

    et_tab = {"X-Jurisdiction": "ET"}
    saved = client.post("/api/transcriptions", json={"title": "ET claim"}, headers=et_tab)
    assert saved.status_code == 200
    client.post("/api/transcriptions", json={"title": "IAC appeal"})  # no header: the remembered IAC

    def titles(jurisdiction):
        return sorted(t.title for t in interface_dictation.fetch_transcriptions_metadata(judge.id, None, jurisdiction.id))

    assert titles(et) == ["ET claim"]
    assert titles(iac) == ["IAC appeal"]
    listed_in_et = client.get("/api/transcriptions-metadata", headers=et_tab).json()
    assert [t["title"] for t in listed_in_et] == ["ET claim"]


@pytest.mark.parametrize(
    ("body", "status"),
    [({}, 422), ({"code": ""}, 422), ({"code": "  "}, 422), ({"code": "X" * 17}, 422), ({"code": " et "}, 200)],
)
def test_a_choice_names_a_jurisdiction_code(jurisdiction_db, judge_in_both, jurisdiction_app, body, status):
    judge, _iac, _et = judge_in_both
    assert client_as(jurisdiction_app, jurisdiction_db, judge).put("/api/jurisdictions/active", json=body).status_code == status


def test_a_choice_that_cannot_be_stored_is_refused(jurisdiction_db, judge_in_both, jurisdiction_app, monkeypatch):
    judge, _iac, _et = judge_in_both
    client = client_as(jurisdiction_app, jurisdiction_db, judge)
    monkeypatch.setattr("transcribe_api.api.routes_jurisdictions.remember_choice", lambda user_id, jid: False)

    refused = client.put("/api/jurisdictions/active", json={"code": "ET"})

    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "jurisdiction_not_available"
    assert remember_choice(uuid4(), _et.id) is False  # the real one, for no such user


def test_a_remembered_choice_the_user_has_since_left_is_not_obeyed(jurisdiction_db, judge_in_both, jurisdiction_app):
    """Stored without re-checking membership: a choice an admin's removal overtakes is
    ignored when read, so the judge is asked to choose again, not put in it."""
    judge, _iac, _et = judge_in_both
    sscs = add_jurisdiction(jurisdiction_db, "SSCS")
    assert remember_choice(judge.id, sscs.id) is True
    assert client_as(jurisdiction_app, jurisdiction_db, judge).get("/api/jurisdictions/me").json()["status"] == "choose"
