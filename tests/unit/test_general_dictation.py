"""DIAAT-333: a judge in a jurisdiction with no template yet dictates into the general
section and copies the text out; leaving part-way keeps what they dictated.

Dictation itself is unchanged: the same live draft, in the jurisdiction's own space.
What a jurisdiction without the decision template cannot use, the IAC's form,
document content and decision document, is tested in test_jurisdictions.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.unit.jurisdiction_fixtures import (
    IAC_JURISDICTION_ID,
    add_jurisdiction,
    add_member,
    add_user,
    client_as,
)
from transcribe_api.domain.models_dictation import LiveTranscriptDraft

GENERAL = {
    "background": [{"speaker": "Judge", "text": "The claimant was dismissed.", "timestamp": "00:01"}],
    "evidence": [],
    "facts": [],
}


@pytest.fixture
def et_judge(jurisdiction_db):
    et = add_jurisdiction(jurisdiction_db, "ET")
    judge = add_user(jurisdiction_db)
    add_member(jurisdiction_db, judge, et)
    return judge, et


@pytest.mark.real_jurisdictions
def test_an_et_judges_draft_is_saved_restored_and_finished_in_the_et(
    jurisdiction_db, et_judge, jurisdiction_app, mocker
):
    """Their only jurisdiction, with no header: as a tab sends before it knows one."""
    judge, et = et_judge
    upsert = mocker.patch("transcribe_api.api.routes_dictation.upsert_live_draft")
    get = mocker.patch(
        "transcribe_api.api.routes_dictation.get_live_draft",
        return_value=LiveTranscriptDraft(
            user_id=judge.id,
            jurisdiction_id=et.id,
            transcript=GENERAL,
            form_data={"notes": "kept"},
            updated_datetime=datetime.now(UTC),
        ),
    )
    delete = mocker.patch("transcribe_api.api.routes_dictation.delete_live_draft")
    client = client_as(jurisdiction_app, jurisdiction_db, judge)

    assert client.put("/api/live-draft", json={"transcript": GENERAL, "form_data": {"notes": "kept"}}).status_code == 204
    restored = client.get("/api/live-draft")
    assert client.delete("/api/live-draft").status_code == 204

    assert upsert.call_args.args == (judge.id, et.id, GENERAL, {"notes": "kept"})
    assert get.call_args.args == (judge.id, et.id)
    assert delete.call_args.args == (judge.id, et.id)
    assert restored.status_code == 200
    assert (restored.json()["transcript"], restored.json()["form_data"]) == (GENERAL, {"notes": "kept"})


def test_each_jurisdiction_keeps_its_own_draft_on_postgres(fresh_postgres, monkeypatch):
    """The real upsert and lookup: a judge in two jurisdictions has a draft in each,
    and finishing one leaves the other."""
    from sqlmodel import Session

    from transcribe_api.domain import interface_dictation
    from transcribe_api.domain.models_dictation import Jurisdiction, User

    monkeypatch.setattr(interface_dictation, "engine", fresh_postgres)
    with Session(fresh_postgres) as session:
        user = User(email="judge@example.test", azure_user_id=str(uuid4()))
        et = Jurisdiction(code="ET", name="Employment Tribunal", settings={})
        session.add_all([user, et])
        session.commit()
        user_id, et_id = user.id, et.id
    iac_id = IAC_JURISDICTION_ID

    interface_dictation.upsert_live_draft(user_id, et_id, GENERAL, {})
    interface_dictation.upsert_live_draft(user_id, iac_id, {"background": [], "evidence": [], "facts": []}, {"a": 1})
    interface_dictation.upsert_live_draft(user_id, et_id, {**GENERAL, "facts": [{"text": "more"}]}, {})

    assert interface_dictation.get_live_draft(user_id, et_id).transcript["facts"] == [{"text": "more"}]
    assert interface_dictation.get_live_draft(user_id, iac_id).form_data == {"a": 1}
    interface_dictation.delete_live_draft(user_id, et_id)
    assert interface_dictation.get_live_draft(user_id, et_id) is None
    assert interface_dictation.get_live_draft(user_id, iac_id) is not None
