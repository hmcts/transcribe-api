"""A recording's history for its owner, and who made each edit (DIAAT-395).

Real authentication and the real recording routes on SQLite: what a Judge does to their own recording
(view, edit, accept, rename a speaker, roll back, merge) is audited with the actor and the role they
acted as; ``GET /api/v1/jobs/{id}/history`` reads it, newest first, paged, for the owner only (anyone
else gets the answer for a recording they cannot see), and no transcript text is anywhere in the
answer or in what was audited.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from tests.unit import auth_fixtures as af
from tests.unit.admin_fixtures import ADMIN, JUDGE, LTM, SUPER, make_world
from transcribe_api.domain.models_dictation import AuditLog
from transcribe_api.domain.models_recording import DialogueEntry, JobStatus, SpeechBatchJob

pytestmark = pytest.mark.real_jurisdictions

SPOKEN_1 = "the appellant alpha said something confidential"
SPOKEN_2 = "and the respondent bravo answered in confidence"
CORRECTED = "TOPSECRETCORRECTION replaced alpha"
NEW_SPEAKER = "Zaphod Renamed"


@pytest.fixture
def world(jurisdiction_db, monkeypatch):
    yield make_world(jurisdiction_db, monkeypatch)
    af.unstub_entra()


@pytest.fixture
def cast(world):
    with Session(world.engine) as session:  # the ET uses hearing recordings too, so it is not the jurisdiction that refuses
        et = session.get(type(world.et), world.et.id)
        et.settings = {"features": {"hearing_recordings": True}}
        session.add(et)
        session.commit()
    return type(
        "Cast",
        (),
        {
            "ivy": world.person("Ivy Judge", ("IAC", JUDGE), ("ET", JUDGE)),
            "joe": world.person("Joe Judge", ("IAC", JUDGE)),
            "jadmin": world.person("Admin IAC", ("IAC", ADMIN)),
            "jsuper": world.person("Super", (None, SUPER)),
            "ltm": world.person("Legal IAC", ("IAC", LTM)),
            "sysadmin": world.person("Sysadmin", sysadmin=True),
            "sysjudge": world.person("Sysadmin Judge", ("IAC", JUDGE), sysadmin=True),
            "nobody": world.person("Nobody"),
        },
    )


@pytest.fixture
def job(world, cast) -> UUID:
    entries = [
        DialogueEntry(id="seg_a", speaker="Speaker 1", text=SPOKEN_1, start_time=0.0, end_time=2.0, confidence=0.4),
        DialogueEntry(id="seg_b", speaker="Speaker 2", text=SPOKEN_2, start_time=2.0, end_time=4.0, confidence=0.5),
        DialogueEntry(id="seg_c", speaker="Speaker 1", text="a third segment charlie", start_time=4.0, end_time=5.0),
    ]
    with Session(world.engine) as session:
        row = SpeechBatchJob(
            audio_url="https://example.invalid/a.wav",
            user_id=cast.ivy.user_id,
            jurisdiction_id=world.iac.id,
            status=JobStatus.SUCCEEDED,
            dialogue_entries=[e.model_dump() for e in entries],
        )
        session.add(row)
        session.commit()
        return row.id


def history(person, job_id, *, role=JUDGE, at="IAC", **params):
    return person.get(f"/api/v1/jobs/{job_id}/history", role=role, jurisdiction=at, params=params)


def edit_everything(cast, job) -> None:
    ivy = cast.ivy
    base = f"/api/v1/jobs/{job}"
    assert ivy.get(base, role=JUDGE, jurisdiction="IAC").status_code == 200
    assert ivy.patch(f"{base}/segments/seg_a", role=JUDGE, jurisdiction="IAC", json={"corrected_text": CORRECTED}).status_code == 200
    assert ivy.post(f"{base}/segments/seg_b/accept", role=JUDGE, jurisdiction="IAC").status_code == 200
    assert ivy.patch(
        f"{base}/segments/seg_c/speaker", role=JUDGE, jurisdiction="IAC", json={"speaker": NEW_SPEAKER, "apply_to_all": True}
    ).status_code == 200
    assert ivy.post(f"{base}/segments/seg_a/history/0/rollback", role=JUDGE, jurisdiction="IAC").status_code == 200
    assert ivy.post(f"{base}/segments/seg_b/rollback", role=JUDGE, jurisdiction="IAC").status_code == 200


# --- what is audited -------------------------------------------------------------------------------------------


def test_a_judges_own_actions_are_in_the_history_newest_first_with_who_and_when(world, cast, job):
    edit_everything(cast, job)

    response = history(cast.ivy, job)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "total"} and body["total"] == len(body["items"]) == 6
    assert [i["action"] for i in body["items"]] == [
        "transcript.rollback",  # reset of seg_b
        "transcript.rollback",  # back to before the edit of seg_a
        "transcript.rename_speaker",
        "transcript.accept",
        "transcript.edit",
        "transcript.view",
    ]
    for item in body["items"]:
        assert set(item) == {"id", "timestamp", "action", "actor_label", "actor_role", "outcome"}
        assert item["actor_label"] == "Ivy Judge" and item["actor_role"] == JUDGE and item["outcome"] == "success"
    stamps = [i["timestamp"] for i in body["items"]]
    assert stamps == sorted(stamps, reverse=True)


def test_a_dictation_row_that_shares_an_action_name_is_not_in_a_recordings_history(world, cast, job):
    """Dictation's heritage rows are ``transcript.view`` and the like on a ``transcription``: not on a recording,
    whatever object id they carry (review finding)."""
    assert cast.ivy.get(f"/api/v1/jobs/{job}", role=JUDGE, jurisdiction="IAC").status_code == 200
    with Session(world.engine) as session:
        session.add(
            AuditLog(
                event_type="transcript.view",
                user_id=str(cast.ivy.user_id),
                object_type="transcription",
                object_id=str(job),
                outcome="success",
            )
        )
        session.commit()

    body = history(cast.ivy, job).json()

    assert body["total"] == len(body["items"]) == 1  # the recording's own view, not the dictation row
    assert body["items"][0]["action"] == "transcript.view"


def test_the_history_is_paged(world, cast, job):
    edit_everything(cast, job)
    first = history(cast.ivy, job, page=1, page_size=4).json()
    second = history(cast.ivy, job, page=2, page_size=4).json()
    assert (len(first["items"]), len(second["items"]), first["total"], second["total"]) == (4, 2, 6, 6)
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})
    assert second["items"][-1]["action"] == "transcript.view"
    assert history(cast.ivy, job, page=3, page_size=4).json()["items"] == []
    assert history(cast.ivy, job, page_size=0).status_code == 422
    assert history(cast.ivy, job, page_size=201).status_code == 422
    assert history(cast.ivy, job, page=0).status_code == 422


def test_a_job_nothing_has_happened_to_has_an_empty_history(world, cast, job):
    assert history(cast.ivy, job).json() == {"items": [], "total": 0}


def test_a_merge_and_a_baseline_are_edits_too(world, cast, job):
    base = f"/api/v1/jobs/{job}"
    assert cast.ivy.post(f"{base}/segments/seg_a/merge-next", role=JUDGE, jurisdiction="IAC").status_code == 200
    assert cast.ivy.post(
        f"{base}/baseline", role=JUDGE, jurisdiction="IAC", files={"file": ("ref.txt", b"a reference transcript", "text/plain")}
    ).status_code == 200
    items = history(cast.ivy, job).json()["items"]
    assert [i["action"] for i in items] == ["transcript.edit", "transcript.edit"]
    kinds = sorted(
        r.detail["kind"] for r in _audit_rows(world, "transcript.edit")
    )
    assert kinds == ["baseline", "merge"]


def _audit_rows(world, event_type=None) -> list[AuditLog]:
    with Session(world.engine) as session:
        query = select(AuditLog)
        if event_type:
            query = query.where(AuditLog.event_type == event_type)
        return list(session.exec(query).all())


def test_the_audit_rows_of_edits_carry_the_actor_and_the_active_role_and_controlled_values_only(world, cast, job):
    edit_everything(cast, job)

    rows = [r for r in _audit_rows(world) if r.object_id == str(job)]
    assert {r.event_type for r in rows} == {
        "transcript.view",
        "transcript.edit",
        "transcript.accept",
        "transcript.rename_speaker",
        "transcript.rollback",
    }
    for row in rows:
        assert row.user_id == str(cast.ivy.user_id) and row.outcome == "success"
        assert row.active_role == JUDGE and row.active_jurisdiction_id == world.iac.id
    edit = next(r for r in rows if r.event_type == "transcript.edit")
    assert edit.detail == {"kind": "segment", "segment": "seg_a"}
    rename = next(r for r in rows if r.event_type == "transcript.rename_speaker")
    assert rename.detail == {"segment": "seg_c", "apply_to_all": True, "segments_changed": 2}


# --- no transcript text anywhere ----------------------------------------------------------------------------------


def test_no_transcript_text_is_in_the_response_or_in_anything_audited(world, cast, job):
    edit_everything(cast, job)
    # A refused attempt by someone else is recorded too.
    cast.joe.get(f"/api/v1/jobs/{job}", role=JUDGE, jurisdiction="IAC")

    response = history(cast.ivy, job)
    assert response.status_code == 200
    everything_audited = json.dumps(
        [
            {"event": r.event_type, "detail": r.detail, "resource": r.resource, "object": r.object_id, "type": r.object_type}
            for r in _audit_rows(world)
        ],
        default=str,
    )
    for text in (SPOKEN_1, SPOKEN_2, CORRECTED, NEW_SPEAKER, "alpha", "bravo", "charlie", "TOPSECRET", "confidential", "Speaker 1"):
        assert text not in response.text, text
        assert text not in everything_audited, text
    # What the response does say about people is the owner's name for what they did, and no email.
    assert cast.ivy.email not in response.text and cast.joe.email not in response.text


# --- the actor on the correction history ------------------------------------------------------------------------------


def test_new_correction_history_entries_name_the_actor_and_the_role_but_the_browser_is_not_sent_them(world, cast, job):
    base = f"/api/v1/jobs/{job}"
    edited = cast.ivy.patch(f"{base}/segments/seg_a", role=JUDGE, jurisdiction="IAC", json={"corrected_text": CORRECTED})
    accepted = cast.ivy.post(f"{base}/segments/seg_b/accept", role=JUDGE, jurisdiction="IAC")
    rolled = cast.ivy.post(f"{base}/segments/seg_a/history/0/rollback", role=JUDGE, jurisdiction="IAC")
    assert edited.status_code == accepted.status_code == rolled.status_code == 200

    with Session(world.engine) as session:
        stored = session.get(SpeechBatchJob, job).dialogue_entries
    by_id = {e["id"]: e for e in stored}
    history_a = by_id["seg_a"]["correction_history"]
    assert [h["kind"] for h in history_a] == ["segment", "rollback"]
    assert all(h["actor_id"] == str(cast.ivy.user_id) and h["actor_role"] == JUDGE for h in history_a)
    (accept,) = by_id["seg_b"]["correction_history"]
    assert accept["kind"] == "accept_all" and accept["actor_id"] == str(cast.ivy.user_id)
    # The per-segment history keeps working and its response shape is unchanged.
    for entry in rolled.json()["dialogue_entries"]:
        for h in entry.get("correction_history") or []:
            assert "actor_id" not in h and "actor_role" not in h
            assert set(h) <= {
                "timestamp", "kind", "previous_text", "new_text", "start_word_index", "end_word_index", "previous_phrase", "new_phrase",
            }


def test_an_old_correction_entry_without_an_actor_still_reads(world, cast):
    from transcribe_api.domain.models_recording import CorrectionEntry

    old = CorrectionEntry.model_validate(
        {"timestamp": "2026-01-01T00:00:00+00:00", "kind": "segment", "previous_text": "a", "new_text": "b"}
    )
    assert old.actor_id is None and old.actor_role is None


# --- who may read it -----------------------------------------------------------------------------------------------------------


def test_another_judge_gets_the_answer_for_a_recording_they_cannot_see(world, cast, job):
    other = history(cast.joe, job)
    unknown = history(cast.joe, uuid4())
    assert other.status_code == unknown.status_code == 404
    assert other.json() == unknown.json() == {"detail": "Job not found"}


def test_the_owner_acting_in_another_jurisdiction_is_told_the_same(world, cast, job):
    elsewhere = history(cast.ivy, job, role=JUDGE, at="ET")
    assert elsewhere.status_code == 404 and elsewhere.json() == {"detail": "Job not found"}


@pytest.mark.parametrize(
    ("who", "role", "at"),
    [("jadmin", ADMIN, "IAC"), ("jsuper", SUPER, None), ("ltm", LTM, "IAC"), ("sysadmin", None, None), ("nobody", None, None)],
)
def test_nobody_but_a_judge_reads_any_history(world, cast, job, who, role, at):
    edit_everything(cast, job)
    response = history(getattr(cast, who), job, role=role, at=at)
    assert response.status_code == 403
    assert "TOPSECRET" not in response.text


def test_a_system_administrator_acting_as_a_judge_is_not_the_owner(world, cast, job):
    response = history(cast.sysjudge, job)
    assert response.status_code == 404 and response.json() == {"detail": "Job not found"}


def test_the_history_needs_a_token(world, cast, job):
    assert world.client.get(f"/api/v1/jobs/{job}/history").status_code == 401


def test_someone_elses_attempt_shows_in_the_owners_history_without_saying_who(world, cast, job):
    probe = cast.joe.get(f"/api/v1/jobs/{job}", role=JUDGE, jurisdiction="IAC")
    assert probe.status_code == 404
    cast.ivy.get(f"/api/v1/jobs/{job}", role=JUDGE, jurisdiction="IAC")

    items = history(cast.ivy, job).json()["items"]
    assert [(i["action"], i["outcome"], i["actor_label"], i["actor_role"]) for i in items] == [
        ("transcript.view", "success", "Ivy Judge", JUDGE),
        ("transcript.view", "denied", "Another user", None),
    ]
    assert "Joe" not in json.dumps(items)


def test_reading_the_history_is_not_itself_a_recorded_action(world, cast, job):
    history(cast.ivy, job)
    history(cast.ivy, job)
    assert history(cast.ivy, job).json()["total"] == 0


def test_the_history_works_with_dictation_switched_off(world, cast, job, monkeypatch):
    """Hearing recordings are not dictation: the off-switch does not touch them."""
    from transcribe_api.domain import feature_flags

    monkeypatch.setattr(feature_flags, "_stored_value", lambda name: (False, True))
    assert history(cast.ivy, job).status_code == 200
