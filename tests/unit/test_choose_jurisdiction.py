"""DIAAT-332, DIAAT-390: a judge who sits in more than one jurisdiction works in the one their
context names.

They choose after signing in (PUT /api/me/context: tests/unit/test_session_api.py) and can
switch later: each token carries one context, and what a request does follows it. Each document
belongs to the jurisdiction it was produced in. Real lookups on SQLite.
"""

from __future__ import annotations

import pytest

from tests.unit.jurisdiction_fixtures import (
    add_jurisdiction,
    add_member,
    add_user,
    client_as,
)
from transcribe_api.domain import interface_dictation

pytestmark = pytest.mark.real_jurisdictions


@pytest.fixture
def judge_in_both(jurisdiction_db):
    iac = add_jurisdiction(jurisdiction_db, "IAC", settings={"template": "iac_decision"})
    et = add_jurisdiction(jurisdiction_db, "ET")
    judge = add_user(jurisdiction_db)
    add_member(jurisdiction_db, judge, iac)
    add_member(jurisdiction_db, judge, et)
    return judge, iac, et


def test_the_template_follows_the_context_the_judge_switched_to(
    jurisdiction_db, judge_in_both, jurisdiction_app, monkeypatch
):
    judge, _iac, _et = judge_in_both
    monkeypatch.setattr(
        "transcribe_api.api.routes_dictation.get_document_content",
        lambda _jurisdiction_id: {"hearing_types": [], "legal_frameworks": [], "anonymity": {}},
    )
    # In the ET the IAC's document content is not theirs...
    et_client = client_as(jurisdiction_app, jurisdiction_db, judge, ctx=("ET", "Judge"))
    assert et_client.get("/api/document-content").status_code == 404
    # ...and it is again once they have switched back (a new token, a new context).
    iac_client = client_as(jurisdiction_app, jurisdiction_db, judge, ctx=("IAC", "Judge"))
    assert iac_client.get("/api/jurisdictions/current").json()["code"] == "IAC"
    assert iac_client.get("/api/document-content").status_code == 200


def test_a_user_with_no_context_chosen_works_in_none(jurisdiction_db, judge_in_both, jurisdiction_app, mocker):
    judge, _iac, _et = judge_in_both
    fetch = mocker.patch("transcribe_api.api.routes_dictation.fetch_transcriptions_metadata", return_value=[])
    client = client_as(jurisdiction_app, jurisdiction_db, judge, ctx=None)

    for path in ("/api/transcriptions-metadata", "/api/jurisdictions/current"):
        response = client.get(path)
        assert response.status_code == 403, path
        assert response.json()["detail"]["code"] == "context_required", path
    fetch.assert_not_called()


def test_each_document_belongs_to_the_jurisdiction_it_was_produced_in(jurisdiction_db, judge_in_both, jurisdiction_app):
    """Two tabs, one in each jurisdiction (two contexts, one after the other: the context is the session's
    and a tab naming the other one is refused): each one's work goes to its own."""
    judge, iac, et = judge_in_both
    et_client = client_as(jurisdiction_app, jurisdiction_db, judge, ctx=("ET", "Judge"))
    et_tab = {"X-Jurisdiction": "ET"}
    saved = et_client.post("/api/transcriptions", json={"title": "ET claim"}, headers=et_tab)
    assert saved.status_code == 200
    # Their other tab, still saying IAC, is out of step with the ET context: nothing is saved.
    stale = et_client.post("/api/transcriptions", json={"title": "IAC appeal"}, headers={"X-Jurisdiction": "IAC"})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "context_changed"

    iac_client = client_as(jurisdiction_app, jurisdiction_db, judge, ctx=("IAC", "Judge"))
    iac_client.post("/api/transcriptions", json={"title": "IAC appeal"})

    def titles(jurisdiction):
        return sorted(t.title for t in interface_dictation.fetch_transcriptions_metadata(judge.id, None, jurisdiction.id))

    assert titles(et) == ["ET claim"]
    assert titles(iac) == ["IAC appeal"]
    # (client_as signs every client of the app in as the latest: back to the ET context.)
    et_client = client_as(jurisdiction_app, jurisdiction_db, judge, ctx=("ET", "Judge"))
    assert [t["title"] for t in et_client.get("/api/transcriptions-metadata", headers=et_tab).json()] == ["ET claim"]
