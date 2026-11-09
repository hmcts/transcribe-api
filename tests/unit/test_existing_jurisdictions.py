"""DIAAT-331: judges in a jurisdiction already using BenchNotes carry on as before.

When a new jurisdiction is added, an IAC judge's documents, draft and legal wording
are still there and unchanged, and they see nothing from the new jurisdiction. Real
lookups on SQLite (jurisdiction_db). That the release keeps existing data unchanged
is tested on Postgres in test_migrations.py (test_0004_and_0005_...).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
import pytz
from sqlmodel import Session

from tests.unit.jurisdiction_fixtures import (
    add_iac_as_migrated,
    add_member,
    client_as,
    load_migration,
    sign_up,
)
from transcribe_api import jurisdictions_admin as admin
from transcribe_api.domain import interface_dictation
from transcribe_api.domain.assignments import load_assignments
from transcribe_api.domain.models_dictation import DocumentContent, Jurisdiction, Transcription

pytestmark = pytest.mark.real_jurisdictions

REPO = Path(__file__).resolve().parents[2]
ET_EXAMPLE = REPO / "docs" / "jurisdictions" / "et.example.json"

# The text the web app showed IAC judges before DIAAT-322 (transcribe-web 00ff378).
IAC_TEXT_BEFORE = {
    "home.intro": (
        "Supporting judicial work through live transcription and enablement of judges to focus on "
        "proceedings and engage with the parties."
    ),
    "help.subtitle": "Getting started with Bench Notes for the First-tier Tribunal Immigration and Asylum Chamber",
    "nav.recordings": "Hearing recordings",
    "records.column_title": "Hearing",
    "records.column_type": "Hearing type",
    "records.search_placeholder": "Search hearings or dates",
    "records.search_hint": "Search recent recordings by hearing title or date.",
    "records.filter_label": "Filter recent recordings by hearing type",
    "records.filter_placeholder": "Filter by hearing type",
    "records.filter_all": "All hearing types",
    "records.no_match": "No hearings matched your current search or filters.",
    "records.open": "Open hearing",
    "records.rename_hint": "Update the title shown in the hearings list.",
    "records.no_transcript": "No saved transcript was found for this hearing.",
    "autosave.record_name": "case record",
    "alerts.speech_lost_guidance": (
        "Pause then restart recording to restart dictation. If this warning comes back, record this decision "
        "in another way and raise with your Support team for investigation."
    ),
}


@pytest.fixture
def iac(jurisdiction_db) -> Jurisdiction:
    return add_iac_as_migrated(jurisdiction_db)


def _iac_judge(engine, iac, email="iac.judge@example.test"):
    """An IAC judge: signed up, and (as migration 0007 made of the old memberships) a Judge in the IAC."""
    judge = sign_up(engine, email)
    add_member(engine, judge, iac)
    return judge


def _assigned(engine, user) -> list[tuple[str, str]]:
    with Session(engine) as session:
        return [(a.jurisdiction_code, a.role) for a in load_assignments(session, user.id)]


def _add_the_et(engine) -> None:
    with Session(engine) as session:
        settings = json.loads(ET_EXAMPLE.read_text())
        admin.put_jurisdiction(session, "ET", "Employment Tribunal", settings)
        admin.add_member(session, "ET", "et.judge@example.test")
        admin.add_member(session, "ET", "et.admin@example.test", "admin")
        admin.put_jurisdiction(session, "ET", "Employment Tribunal", settings, stage="live")  # DIAAT-345


def test_the_iacs_wording_is_the_text_its_judges_saw_before():
    assert load_migration("0004_jurisdictions.py").IAC_SETTINGS["wording"] == IAC_TEXT_BEFORE


def test_adding_a_jurisdiction_changes_nothing_an_iac_judge_sees(jurisdiction_db, iac, jurisdiction_app):
    judge = _iac_judge(jurisdiction_db, iac)
    interface_dictation.save_transcription(Transcription(title="Smith v SSHD"), judge.id, iac.id)
    client = client_as(jurisdiction_app, jurisdiction_db, judge)
    before = (client.get("/api/jurisdictions/current").json(), client.get("/api/transcriptions-metadata").json())

    _add_the_et(jurisdiction_db)

    after = (client.get("/api/jurisdictions/current").json(), client.get("/api/transcriptions-metadata").json())
    assert after == before
    assert before[0]["code"] == "IAC"
    assert before[0]["wording"] == IAC_TEXT_BEFORE
    assert [t["title"] for t in before[1]] == ["Smith v SSHD"]


def test_adding_a_jurisdiction_leaves_the_iac_row_and_assignments_alone(jurisdiction_db, iac):
    judge = _iac_judge(jurisdiction_db, iac)
    with Session(jurisdiction_db) as session:
        before = session.get(Jurisdiction, iac.id).model_dump()

    _add_the_et(jurisdiction_db)

    with Session(jurisdiction_db) as session:
        assert session.get(Jurisdiction, iac.id).model_dump() == before
    assert _assigned(jurisdiction_db, judge) == [("IAC", "Judge")]


def test_an_iac_judge_sees_nothing_from_the_new_jurisdiction(jurisdiction_db, iac, jurisdiction_app):
    _add_the_et(jurisdiction_db)
    judge = _iac_judge(jurisdiction_db, iac)
    et_judge = sign_up(jurisdiction_db, "et.judge@example.test")  # claims the pending ET assignment
    with Session(jurisdiction_db) as session:
        [et_assignment] = load_assignments(session, et_judge.id)
    et_id = et_assignment.jurisdiction_id
    interface_dictation.save_transcription(Transcription(title="ET claim"), judge.id, et_id)  # even their own
    interface_dictation.save_transcription(Transcription(title="IAC appeal"), judge.id, iac.id)
    client = client_as(jurisdiction_app, jurisdiction_db, judge)

    assert [t["title"] for t in client.get("/api/transcriptions-metadata").json()] == ["IAC appeal"]
    assert client.get("/api/jurisdictions/current", headers={"X-Jurisdiction": "ET"}).status_code == 409
    assert client.get("/api/jurisdictions/current").json()["code"] == "IAC"
    # A tab naming the ET is out of step with the context the judge acts in (the IAC).
    asked = client.get("/api/transcriptions-metadata", headers={"X-Jurisdiction": "ET"})
    assert asked.status_code == 409
    assert asked.json()["detail"]["code"] == "context_changed"


def test_new_users_get_no_jurisdiction_whatever_exists(jurisdiction_db, iac):
    """DIAAT-373: nobody is enrolled automatically, not even into the IAC as before jurisdictions."""
    _add_the_et(jurisdiction_db)
    newcomer = sign_up(jurisdiction_db, "new.iac.judge@example.test")
    assert _assigned(jurisdiction_db, newcomer) == []


def test_an_iac_judges_legal_wording_is_served_unchanged(jurisdiction_db, iac, jurisdiction_app):
    """The real content loader: the database seeds itself from the bundled IAC wording,
    and the IAC judge gets it all, with another jurisdiction present."""
    bundled = json.loads((REPO / "src" / "transcribe_api" / "data" / "document_content.json").read_text())
    _add_the_et(jurisdiction_db)
    judge = _iac_judge(jurisdiction_db, iac)

    response = client_as(jurisdiction_app, jurisdiction_db, judge).get("/api/document-content")

    assert response.status_code == 200
    body = response.json()
    for part in ("hearing_types", "legal_frameworks", "anonymity"):
        assert body[part] == bundled[part], part
    # Served from the database (seeded on first read), not the file fallback.
    with Session(jurisdiction_db) as session:
        assert session.get(DocumentContent, 1).content == bundled


def test_a_tab_open_before_the_release_carries_on(jurisdiction_db, iac, jurisdiction_app, mocker):
    """A page loaded before the deploy sends no X-Jurisdiction: the IAC judge's context (their only
    jurisdiction, selected automatically) is used, so their draft saves where it always did."""
    judge = _iac_judge(jurisdiction_db, iac)
    upsert = mocker.patch("transcribe_api.api.routes_dictation.upsert_live_draft")

    response = client_as(jurisdiction_app, jurisdiction_db, judge).put("/api/live-draft", json={"transcript": {"background": []}, "form_data": {}})

    assert response.status_code == 204
    assert upsert.call_args.args[:2] == (judge.id, iac.id)


def test_the_list_shows_a_record_as_before(jurisdiction_db, iac):
    """Listing in a jurisdiction does not change what a listed record says, dates included:
    stored UTC noon in summer is 13:00 in London."""
    judge = _iac_judge(jurisdiction_db, iac)
    saved = interface_dictation.save_transcription(
        Transcription(title="A v B", created_datetime=datetime(2026, 7, 1, 12, 0)), judge.id, iac.id
    )

    [listed] = interface_dictation.fetch_transcriptions_metadata(judge.id, pytz.timezone("Europe/London"), iac.id)

    assert (listed.id, listed.title) == (saved.id, "A v B")
    assert listed.created_datetime.isoformat() == "2026-07-01T13:00:00+01:00"
