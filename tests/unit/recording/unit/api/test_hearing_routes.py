"""DIAAT-428: hearings with several recording days, through the real recording routes on SQLite.

POST /jobs makes a new one-day hearing (its id is the job's) or, with ``hearing_id`` and the
``multi_day_hearings`` flag on, adds a day to the caller's own hearing in the jurisdiction they work in
(anyone else's, or another jurisdiction's, is the 404 of an unknown hearing, and the refusal is audited).
GET /hearings lists one summary per hearing, without transcripts; GET /hearings/{id} returns every day's
transcript in order. A day's date can be changed (audited); deleting a day deletes its recording, and the
hearing with its last day; DELETE /hearings/{id} deletes every day and says so if one could not be.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.unit.jurisdiction_fixtures import IAC_JURISDICTION_ID
from tests.unit.recording.unit.async_upload_helpers import USER_ID, add_job, new_engine
from transcribe_api.api.app import create_app
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.dependencies_recording import get_current_user
from transcribe_api.domain.models_recording import Hearing, JobStatus, SpeechBatchJob, User
from transcribe_api.runtime import settings_dictation, settings_recording
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.stt import local_storage

OTHER_USER = UUID("00000000-0000-4000-8000-0000000000b2")
OTHER_JURISDICTION = UUID("00000000-0000-4000-8000-0000000000c3")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SPOKEN = "the appellant said something confidential"


@pytest.fixture
def engine(tmp_path):
    engine = new_engine(tmp_path / "hearings.db")
    yield engine
    engine.dispose()


@pytest.fixture
def multi_day(monkeypatch):
    monkeypatch.setenv("MULTI_DAY_HEARINGS", "true")
    settings_dictation.get_settings.cache_clear()


@pytest.fixture
def client(engine, tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "http://api.test")
    settings_recording.get_settings.cache_clear()

    async def submitted(session, job, *, created=False):
        job.status = JobStatus.SUBMITTED
        session.add(job)
        session.commit()
        session.refresh(job)
        return job

    mocker.patch("transcribe_api.stt.submission.submit_existing_job", new=submitted)
    from transcribe_api.api.routes_recording import limiter

    limiter.reset()
    app = create_app()
    user = AuthenticatedUser(
        db_user=User(id=USER_ID, azure_user_id="oid", email="judge@example.test", role="Judge", name="Ivy Judge"),
        ctx=Context("IAC", "Judge"),
    )
    app.dependency_overrides[get_current_user] = lambda: user

    def session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = session
    return TestClient(app, raise_server_exceptions=False)


def _hearing(engine, *, user_id: UUID = USER_ID, jurisdiction_id: UUID = IAC_JURISDICTION_ID, **metadata) -> UUID:
    hearing = Hearing(user_id=user_id, jurisdiction_id=jurisdiction_id, metadata_=metadata)
    with Session(engine) as session:
        session.add(hearing)
        session.commit()
        return hearing.id


def _day(
    engine, hearing_id: UUID | None, *, day: date | None = None, created: datetime = T0, **fields
) -> SpeechBatchJob:
    fields.setdefault("status", JobStatus.SUCCEEDED)
    if fields["status"] == JobStatus.SUCCEEDED:
        fields.setdefault(
            "dialogue_entries",
            [
                {
                    "id": "seg_1",
                    "speaker": "Speaker 1",
                    "text": SPOKEN,
                    "start_time": 0.0,
                    "end_time": 1.0,
                    "confidence": 0.5,
                }
            ],
        )
    return add_job(
        engine,
        hearing_id=hearing_id,
        hearing_date=day,
        created_datetime=created,
        source_blob_path=None,
        jurisdiction_id=fields.pop("jurisdiction_id", IAC_JURISDICTION_ID),
        **fields,
    )


def _submit(client, **body):
    return client.post("/api/v1/jobs", json={"audio_url": "https://storage.example.com/a.wav", **body})


def _rows(audit_capture, action: str) -> list[dict]:
    return [row for row in audit_capture.rows if row["action"] == action]


def _job(engine, job_id) -> SpeechBatchJob | None:
    with Session(engine) as session:
        return session.get(SpeechBatchJob, UUID(str(job_id)))


# POST /jobs --------------------------------------------------------------------------------------------


def test_a_job_submitted_without_a_hearing_is_a_new_one_day_hearing_with_the_jobs_id(client, engine, audit_capture):
    response = _submit(
        client, metadata={"case_reference": "PA/1", "tribunal": "Taylor House", "audio_file_name": "day1.mp3"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["hearing_id"] == body["job_id"]
    with Session(engine) as session:
        hearing = session.get(Hearing, UUID(body["job_id"]))
        assert (hearing.user_id, hearing.jurisdiction_id) == (USER_ID, IAC_JURISDICTION_ID)
        assert hearing.metadata_ == {"case_reference": "PA/1", "tribunal": "Taylor House"}
    (submit,) = _rows(audit_capture, "job.submit")
    assert submit["detail"] == {"hearing_id": body["job_id"]}
    assert _rows(audit_capture, "hearing.add_recording") == []


def test_the_hearing_date_defaults_to_the_day_of_the_upload_in_london(client, engine, mocker):
    mocker.patch("transcribe_api.domain.hearings._now", return_value=datetime(2026, 7, 1, 23, 30, tzinfo=UTC))
    body = _submit(client).json()
    assert body["hearing_date"] == "2026-07-02"
    assert _job(engine, body["job_id"]).hearing_date == date(2026, 7, 2)


def test_the_hearing_date_can_be_given_and_any_date_is_accepted(client, engine):
    body = _submit(client, hearing_date="2019-02-28").json()
    assert body["hearing_date"] == "2019-02-28"


def test_an_old_submission_without_the_new_fields_works_as_before_with_the_flag_off(client, engine):
    response = _submit(client, locale="en-GB", metadata={"audio_file_name": "a.mp3"})
    assert response.status_code == 201
    assert response.json()["status"] == "submitted"


def test_a_recording_is_added_to_the_callers_own_hearing(client, engine, audit_capture, multi_day):
    hearing_id = _hearing(engine, case_reference="PA/1")
    _day(engine, hearing_id, day=date(2026, 11, 2))

    response = _submit(
        client, hearing_id=str(hearing_id), hearing_date="2026-11-03", metadata={"audio_file_name": "day2.mp3"}
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["hearing_id"], body["hearing_date"]) == (str(hearing_id), "2026-11-03")
    assert _job(engine, body["job_id"]).hearing_id == hearing_id
    (added,) = _rows(audit_capture, "hearing.add_recording")
    assert (added["outcome"], added["object_type"], added["object_id"]) == ("success", "hearing", hearing_id)
    assert added["detail"] == {"job_id": body["job_id"]}
    (submit,) = _rows(audit_capture, "job.submit")
    assert submit["detail"] == {"hearing_id": str(hearing_id)}


def test_a_recording_can_be_added_while_another_day_is_still_processing(client, engine, multi_day):
    hearing_id = _hearing(engine)
    _day(engine, hearing_id, status=JobStatus.RUNNING)
    assert _submit(client, hearing_id=str(hearing_id)).status_code == 201


def test_a_recording_is_added_to_a_job_from_before_hearings_which_becomes_a_stored_hearing(client, engine, multi_day):
    legacy = _day(engine, None, created=datetime(2026, 7, 1, 23, 30, tzinfo=UTC))

    response = _submit(client, hearing_id=str(legacy.id))

    assert response.status_code == 201
    with Session(engine) as session:
        assert session.get(Hearing, legacy.id) is not None
        first = session.get(SpeechBatchJob, legacy.id)
        assert (first.hearing_id, first.hearing_date) == (legacy.id, date(2026, 7, 2))


@pytest.mark.parametrize("owner", ["another user", "another jurisdiction", "nobody"])
def test_a_recording_cannot_be_added_to_a_hearing_that_is_not_the_callers_here(
    client, engine, audit_capture, multi_day, owner
):
    if owner == "another user":
        hearing_id = _hearing(engine, user_id=OTHER_USER)
    elif owner == "another jurisdiction":
        hearing_id = _hearing(engine, jurisdiction_id=OTHER_JURISDICTION)
    else:
        hearing_id = uuid4()

    response = _submit(client, hearing_id=str(hearing_id))

    assert response.status_code == 404
    assert response.json()["detail"] == "Hearing not found"
    with Session(engine) as session:
        assert session.query(SpeechBatchJob).count() == 0
    assert _rows(audit_capture, "job.submit") == []
    if owner == "another user":
        (refused,) = _rows(audit_capture, "hearing.add_recording")
        assert (refused["outcome"], refused["object_type"], refused["object_id"], refused["detail"]) == (
            "denied", "hearing", hearing_id, {"reason": "not_owner"},
        )  # fmt: skip


def test_adding_a_recording_to_a_hearing_is_refused_while_the_flag_is_off(client, engine):
    hearing_id = _hearing(engine)
    response = _submit(client, hearing_id=str(hearing_id))
    assert response.status_code == 403
    with Session(engine) as session:
        assert session.query(SpeechBatchJob).count() == 0


# GET /hearings -----------------------------------------------------------------------------------------


def test_the_list_has_one_summary_per_hearing_with_its_days_and_no_transcript(client, engine):
    hearing_id = _hearing(engine, case_reference="PA/1", tribunal="Taylor House")
    day_2 = _day(
        engine,
        hearing_id,
        day=date(2026, 11, 3),
        created=T0,
        status=JobStatus.RUNNING,
        metadata_={"audio_file_name": "b.mp3"},
    )
    day_1 = _day(
        engine,
        hearing_id,
        day=date(2026, 11, 2),
        created=T0 - timedelta(hours=1),
        audio_duration_seconds=60.0,
        metadata_={"audio_file_name": "a.mp3"},
    )
    failed = _day(
        engine,
        hearing_id,
        day=date(2026, 11, 4),
        created=T0 - timedelta(hours=2),
        status=JobStatus.FAILED,
        error_message="Speech failed",
    )
    _day(engine, _hearing(engine, user_id=OTHER_USER), user_id=OTHER_USER)

    response = client.get("/api/v1/hearings")

    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["limit"], body["offset"]) == (1, 20, 0)
    (summary,) = body["hearings"]
    assert {k: summary[k] for k in ("hearing_id", "case_reference", "tribunal", "status", "day_count", "days_complete", "days_failed")} == {
        "hearing_id": str(hearing_id), "case_reference": "PA/1", "tribunal": "Taylor House", "status": "processing",
        "day_count": 3, "days_complete": 1, "days_failed": 1,
    }  # fmt: skip
    assert [(d["job_id"], d["day_number"], d["hearing_date"], d["status"]) for d in summary["days"]] == [
        (str(day_1.id), 1, "2026-11-02", "succeeded"),
        (str(day_2.id), 2, "2026-11-03", "running"),
        (str(failed.id), 3, "2026-11-04", "failed"),
    ]
    assert summary["days"][0]["audio_file_name"] == "a.mp3"
    assert summary["days"][0]["audio_duration_seconds"] == 60.0
    assert summary["days"][2]["error_message"] == "Speech failed"
    assert SPOKEN not in response.text and "dialogue_entries" not in response.text


def test_the_list_pages_and_includes_jobs_from_before_hearings(client, engine):
    _day(engine, None, created=T0 - timedelta(days=1))
    newest = _day(engine, _hearing(engine), created=T0)
    body = client.get("/api/v1/hearings", params={"limit": 1, "offset": 0}).json()
    assert body["total"] == 2
    assert [h["hearing_id"] for h in body["hearings"]] == [str(newest.hearing_id)]


def test_listing_is_not_a_view(client, engine, audit_capture):
    _day(engine, _hearing(engine))
    client.get("/api/v1/hearings")
    assert audit_capture.rows == []


# GET /hearings/{id} ------------------------------------------------------------------------------------


def test_a_hearing_has_every_days_transcript_in_order_and_one_view_is_audited(client, engine, audit_capture):
    hearing_id = _hearing(engine, case_reference="PA/1")
    day_2 = _day(engine, hearing_id, day=date(2026, 11, 3))
    day_1 = _day(engine, hearing_id, day=date(2026, 11, 2))

    response = client.get(f"/api/v1/hearings/{hearing_id}")

    assert response.status_code == 200
    body = response.json()
    assert (body["hearing_id"], body["status"], body["case_reference"], body["day_count"]) == (
        str(hearing_id),
        "completed",
        "PA/1",
        2,
    )
    assert [(d["job_id"], d["day_number"], d["hearing_date"], d["hearing_id"]) for d in body["days"]] == [
        (str(day_1.id), 1, "2026-11-02", str(hearing_id)),
        (str(day_2.id), 2, "2026-11-03", str(hearing_id)),
    ]
    assert body["days"][0]["dialogue_entries"][0]["text"] == SPOKEN
    assert body["days"][0]["needs_review"][0]["job_id"] == str(day_1.id)
    (view,) = audit_capture.rows
    assert (view["action"], view["object_type"], view["object_id"]) == ("transcript.view", "hearing", hearing_id)
    assert view["detail"] == {"job_ids": [str(day_1.id), str(day_2.id)]}


def test_one_day_of_a_hearing_can_be_asked_for(client, engine, audit_capture):
    hearing_id = _hearing(engine)
    day_1 = _day(engine, hearing_id, day=date(2026, 11, 2))
    day_2 = _day(engine, hearing_id, day=date(2026, 11, 3))

    body = client.get(f"/api/v1/hearings/{hearing_id}", params={"day": str(day_2.id)}).json()

    assert [(d["job_id"], d["day_number"], d["dialogue_entries"] is None) for d in body["days"]] == [
        (str(day_1.id), 1, True),
        (str(day_2.id), 2, False),
    ]
    (view,) = audit_capture.rows
    assert view["detail"] == {"job_ids": [str(day_2.id)]}
    assert client.get(f"/api/v1/hearings/{hearing_id}", params={"day": str(uuid4())}).status_code == 404


def test_a_hearing_with_nothing_transcribed_yet_is_not_a_view(client, engine, audit_capture):
    hearing_id = _hearing(engine)
    _day(engine, hearing_id, status=JobStatus.RUNNING)
    assert client.get(f"/api/v1/hearings/{hearing_id}").json()["status"] == "processing"
    assert audit_capture.rows == []


def test_a_job_from_before_hearings_is_a_hearing_with_its_id(client, engine):
    legacy = _day(engine, None, metadata_={"case_reference": "OLD/1"})
    body = client.get(f"/api/v1/hearings/{legacy.id}").json()
    assert (body["hearing_id"], body["case_reference"], [d["job_id"] for d in body["days"]]) == (
        str(legacy.id),
        "OLD/1",
        [str(legacy.id)],
    )


@pytest.mark.parametrize("owner", ["another user", "another jurisdiction"])
def test_someone_elses_hearing_is_not_found(client, engine, audit_capture, owner):
    if owner == "another user":
        hearing_id = _hearing(engine, user_id=OTHER_USER)
        _day(engine, hearing_id, user_id=OTHER_USER)
    else:
        hearing_id = _hearing(engine, jurisdiction_id=OTHER_JURISDICTION)
        _day(engine, hearing_id, jurisdiction_id=OTHER_JURISDICTION)

    response = client.get(f"/api/v1/hearings/{hearing_id}")

    assert response.status_code == 404
    assert SPOKEN not in response.text
    if owner == "another user":
        (refused,) = _rows(audit_capture, "transcript.view")
        assert (refused["outcome"], refused["object_type"], refused["object_id"]) == ("denied", "hearing", hearing_id)


# PATCH /jobs/{id}/hearing-date -------------------------------------------------------------------------


def test_a_days_date_is_changed_and_the_days_are_reordered(client, engine, audit_capture, multi_day):
    hearing_id = _hearing(engine)
    day_1 = _day(engine, hearing_id, day=date(2026, 11, 2))
    day_2 = _day(engine, hearing_id, day=date(2026, 11, 3))

    response = client.patch(f"/api/v1/jobs/{day_1.id}/hearing-date", json={"hearing_date": "2026-11-04"})

    assert response.status_code == 200
    assert [(d["job_id"], d["hearing_date"]) for d in response.json()["days"]] == [
        (str(day_2.id), "2026-11-03"),
        (str(day_1.id), "2026-11-04"),
    ]
    (edit,) = audit_capture.rows
    assert (edit["action"], edit["outcome"], edit["object_type"], edit["object_id"]) == (
        "hearing.edit",
        "success",
        "hearing",
        hearing_id,
    )
    assert edit["detail"] == {"kind": "hearing_date", "job_id": str(day_1.id)}


def test_changing_a_date_is_refused_while_the_flag_is_off(client, engine):
    day = _day(engine, _hearing(engine), day=date(2026, 11, 2))
    assert client.patch(f"/api/v1/jobs/{day.id}/hearing-date", json={"hearing_date": "2026-11-04"}).status_code == 403
    assert _job(engine, day.id).hearing_date == date(2026, 11, 2)


def test_someone_elses_day_cannot_be_redated(client, engine, multi_day):
    day = _day(engine, _hearing(engine, user_id=OTHER_USER), user_id=OTHER_USER, day=date(2026, 11, 2))
    assert client.patch(f"/api/v1/jobs/{day.id}/hearing-date", json={"hearing_date": "2026-11-04"}).status_code == 404
    assert _job(engine, day.id).hearing_date == date(2026, 11, 2)


# DELETE /jobs/{id} -------------------------------------------------------------------------------------


def test_deleting_a_day_deletes_its_recording_and_keeps_the_hearing(client, engine, audit_capture, multi_day):
    hearing_id = _hearing(engine)
    day_1 = _day(engine, hearing_id, day=date(2026, 11, 2))
    day_2 = _day(engine, hearing_id, day=date(2026, 11, 3))
    local_storage.save(b"wav", day_1.audio_blob_path)

    assert client.delete(f"/api/v1/jobs/{day_1.id}").status_code == 204

    assert _job(engine, day_1.id) is None and _job(engine, day_2.id) is not None
    with pytest.raises(FileNotFoundError):
        local_storage.size(day_1.audio_blob_path)
    with Session(engine) as session:
        assert session.get(Hearing, hearing_id) is not None
    (deleted,) = audit_capture.rows
    assert (deleted["action"], deleted["object_id"], deleted["detail"]) == (
        "job.delete",
        day_1.id,
        {"hearing_id": str(hearing_id)},
    )


def test_deleting_the_last_day_deletes_the_hearing(client, engine, multi_day):
    hearing_id = _hearing(engine)
    day = _day(engine, hearing_id)
    assert client.delete(f"/api/v1/jobs/{day.id}").status_code == 204
    with Session(engine) as session:
        assert session.get(Hearing, hearing_id) is None


def test_a_one_day_hearing_is_deleted_with_the_flag_off(client, engine):
    hearing_id = _hearing(engine)
    day = _day(engine, hearing_id)
    assert client.delete(f"/api/v1/jobs/{day.id}").status_code == 204
    with Session(engine) as session:
        assert session.get(Hearing, hearing_id) is None


def test_deleting_one_day_of_several_is_refused_while_the_flag_is_off(client, engine):
    hearing_id = _hearing(engine)
    day = _day(engine, hearing_id)
    _day(engine, hearing_id)
    assert client.delete(f"/api/v1/jobs/{day.id}").status_code == 403
    assert _job(engine, day.id) is not None


# DELETE /hearings/{id} ---------------------------------------------------------------------------------


def test_deleting_a_hearing_deletes_every_day(client, engine, audit_capture):
    hearing_id = _hearing(engine)
    days = [_day(engine, hearing_id, day=date(2026, 11, d)) for d in (2, 3)]
    for day in days:
        local_storage.save(b"wav", day.audio_blob_path)

    assert client.delete(f"/api/v1/hearings/{hearing_id}").status_code == 204

    assert all(_job(engine, d.id) is None for d in days)
    with Session(engine) as session:
        assert session.get(Hearing, hearing_id) is None
    (deleted,) = _rows(audit_capture, "hearing.delete")
    assert (deleted["outcome"], deleted["object_type"], deleted["object_id"]) == ("success", "hearing", hearing_id)
    assert sorted(r["object_id"] for r in _rows(audit_capture, "job.delete")) == sorted(d.id for d in days)


def test_a_day_that_cannot_be_deleted_keeps_it_and_the_hearing_and_the_delete_can_be_repeated(
    client, engine, mocker, audit_capture
):
    hearing_id = _hearing(engine)
    day_1 = _day(engine, hearing_id, day=date(2026, 11, 2))
    day_2 = _day(engine, hearing_id, day=date(2026, 11, 3))
    real_delete = local_storage.delete

    def refuse_day_2(name):
        if name == day_2.audio_blob_path:
            raise OSError("storage down")
        real_delete(name)

    mocker.patch("transcribe_api.api.routes_recording.local_storage.delete", side_effect=refuse_day_2)
    response = client.delete(f"/api/v1/hearings/{hearing_id}")

    assert response.status_code == 502
    assert _job(engine, day_1.id) is None and _job(engine, day_2.id) is not None
    with Session(engine) as session:
        assert session.get(Hearing, hearing_id) is not None
    (failed,) = _rows(audit_capture, "hearing.delete")
    assert failed["outcome"] == "failed"

    mocker.patch("transcribe_api.api.routes_recording.local_storage.delete", side_effect=real_delete)
    assert client.delete(f"/api/v1/hearings/{hearing_id}").status_code == 204
    with Session(engine) as session:
        assert session.get(Hearing, hearing_id) is None


def test_someone_elses_hearing_cannot_be_deleted(client, engine):
    hearing_id = _hearing(engine, user_id=OTHER_USER)
    day = _day(engine, hearing_id, user_id=OTHER_USER)
    assert client.delete(f"/api/v1/hearings/{hearing_id}").status_code == 404
    assert _job(engine, day.id) is not None


# GET /hearings/{id}/history ----------------------------------------------------------------------------


def test_the_history_of_a_hearing_covers_the_hearing_its_days_and_the_days_deleted_from_it(client, engine):
    from transcribe_api.domain.models_dictation import AuditLog

    hearing_id = _hearing(engine)
    day = _day(engine, hearing_id)
    deleted_day, unrelated = uuid4(), _day(engine, _hearing(engine))
    rows = [
        ("job.submit", deleted_day, "job", {"hearing_id": str(hearing_id)}),
        ("transcript.view", deleted_day, "job", None),
        ("hearing.add_recording", hearing_id, "hearing", {"job_id": str(day.id)}),
        ("job.submit", day.id, "job", {"hearing_id": str(hearing_id)}),
        ("transcript.edit", day.id, "job", {"kind": "segment", "segment": "seg_1"}),
        ("job.delete", deleted_day, "job", {"hearing_id": str(hearing_id)}),
        ("hearing.edit", hearing_id, "hearing", {"kind": "hearing_date", "job_id": str(day.id)}),
        ("transcript.view", unrelated.id, "job", None),
    ]
    with Session(engine) as session:
        for i, (action, object_id, object_type, detail) in enumerate(rows):
            session.add(
                AuditLog(
                    timestamp=T0 + timedelta(minutes=i), event_type=action, user_id=str(USER_ID), outcome="success",
                    object_type=object_type, object_id=str(object_id), detail=detail, active_role="Judge",
                )
            )  # fmt: skip
        session.commit()

    response = client.get(f"/api/v1/hearings/{hearing_id}/history")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 7
    assert [(item["action"], item["job_id"]) for item in body["items"]] == [
        ("hearing.edit", str(day.id)),
        ("job.delete", str(deleted_day)),
        ("transcript.edit", str(day.id)),
        ("job.submit", str(day.id)),
        ("hearing.add_recording", str(day.id)),
        ("transcript.view", str(deleted_day)),
        ("job.submit", str(deleted_day)),
    ]
    assert body["items"][0]["actor_label"] == "Ivy Judge"


def test_someone_elses_hearing_has_no_history_here(client, engine):
    hearing_id = _hearing(engine, user_id=OTHER_USER)
    _day(engine, hearing_id, user_id=OTHER_USER)
    assert client.get(f"/api/v1/hearings/{hearing_id}/history").status_code == 404


def test_the_hearing_actions_are_in_the_audit_vocabulary_and_the_history():
    from typing import get_args

    from transcribe_api.domain.audit_recording import AuditAction, AuditObjectType
    from transcribe_api.domain.job_history import HISTORY_ACTIONS, HISTORY_OBJECT_TYPES
    from transcribe_api.runtime.telemetry_schema import AUDIT_ACTIONS_APP

    actions = {"hearing.add_recording", "hearing.edit", "hearing.delete"}
    assert actions <= set(get_args(AuditAction)) & set(AUDIT_ACTIONS_APP) & set(HISTORY_ACTIONS)
    assert "hearing" in set(get_args(AuditObjectType)) & set(HISTORY_OBJECT_TYPES)
