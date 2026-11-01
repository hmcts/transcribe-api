"""DIAAT-334: nobody can reach another jurisdiction's material, by any route.

Changing a link, or anything a page sends, to name another jurisdiction's record gets
the same 404 as an unknown record. A judge in two jurisdictions reaches the other's
material only by switching. Each blocked attempt is recorded, without case content,
so the product team can see it: a ``security.jurisdiction_blocked`` event (two enums)
and a denied audit record (who, and which record). Real lookups on SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hmcts_otel.testing import capture_telemetry
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import add_jurisdiction, add_member, add_user, client_as
from transcribe_api.domain import interface_dictation
from transcribe_api.domain.models_dictation import AuditLog, Transcription
from transcribe_api.domain.models_recording import JobStatus, SpeechBatchJob

pytestmark = pytest.mark.real_jurisdictions

EVENT = "security.jurisdiction_blocked"
RECORDINGS = {"features": {"hearing_recordings": True}}


@pytest.fixture
def judge_in_two(jurisdiction_db):
    """A judge in the IAC and the SSCS (both with recordings), with a record in each."""
    iac = add_jurisdiction(jurisdiction_db, "IAC", settings={"template": "iac_decision", **RECORDINGS})
    sscs = add_jurisdiction(jurisdiction_db, "SSCS", settings=RECORDINGS)
    judge = add_user(jurisdiction_db)
    add_member(jurisdiction_db, judge, iac)
    add_member(jurisdiction_db, judge, sscs)
    in_iac = interface_dictation.save_transcription(Transcription(title="Smith v SSHD"), judge.id, iac.id)
    in_sscs = interface_dictation.save_transcription(Transcription(title="PIP appeal"), judge.id, sscs.id)
    return judge, iac, sscs, in_iac, in_sscs


def _blocked(telemetry) -> list[dict]:
    return [dict(e) for e in telemetry.events(EVENT)]


def _denied_rows(engine) -> list[AuditLog]:
    with Session(engine) as session:
        return [r for r in session.exec(select(AuditLog)).all() if r.outcome == "denied"]


# Changing a link ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "resource", "action"),
    [
        ("GET", "/api/transcriptions/{id}", "transcription", "read"),
        ("GET", "/api/transcriptions/{id}/document-download", "document", "read"),
        ("GET", "/api/download?transcription_id={id}", "document", "read"),
        ("DELETE", "/api/transcriptions/{id}", "transcription", "delete"),
        ("GET", "/api/transcriptions/{id}/minute-versions", "minute_version", "read"),
        ("GET", "/api/transcriptions/{id}/jobs", "transcription_job", "read"),
        ("GET", "/api/transcriptions/{id}/jobs/{other}/audio", "audio", "read"),
        ("GET", "/api/transcriptions/{id}/tags", "transcription", "read"),
        ("POST", "/api/transcriptions/{id}/tags?tag_name=x", "transcription", "write"),
        ("GET", "/api/transcriptions/{id}/sectioned-transcript", "transcription", "read"),
    ],
)
def test_a_link_changed_to_another_jurisdictions_record_finds_nothing(
    jurisdiction_db, judge_in_two, jurisdiction_app, method, path, resource, action
):
    judge, _iac, _sscs, _in_iac, in_sscs = judge_in_two
    client = client_as(jurisdiction_app, jurisdiction_db, judge)
    url = path.format(id=in_sscs.id, other=uuid4())

    with capture_telemetry() as telemetry:
        response = client.request(method, url, headers={"X-Jurisdiction": "IAC"})

    assert response.status_code == 404
    assert "PIP appeal" not in response.text
    [event] = _blocked(telemetry)
    assert (event["security.resource_type"], event["security.action"]) == (resource, action)
    # Without case content, jurisdiction or record: the audit record says who and what.
    assert not [key for key in event if "jurisdiction" in key]
    assert str(in_sscs.id) not in str(event) and "PIP" not in str(event)
    [row] = _denied_rows(jurisdiction_db)
    assert (row.user_id, row.object_id) == (str(judge.id), str(in_sscs.id))
    assert row.detail.get("jurisdiction_blocked") is True
    with Session(jurisdiction_db) as session:
        assert session.get(Transcription, in_sscs.id) is not None  # nothing deleted


def test_the_same_record_is_reached_in_its_own_jurisdiction(jurisdiction_db, judge_in_two, jurisdiction_app):
    judge, _iac, _sscs, _in_iac, in_sscs = judge_in_two
    client = client_as(jurisdiction_app, jurisdiction_db, judge)

    with capture_telemetry() as telemetry:
        response = client.get(f"/api/transcriptions/{in_sscs.id}", headers={"X-Jurisdiction": "SSCS"})

    assert response.status_code == 200
    assert response.json()["title"] == "PIP appeal"
    assert _blocked(telemetry) == []


def test_a_judge_in_one_jurisdiction_cannot_reach_their_record_left_in_another(jurisdiction_db, jurisdiction_app):
    """Moved from the IAC to the ET only: their old IAC record is not theirs to see here."""
    iac, et = add_jurisdiction(jurisdiction_db, "IAC"), add_jurisdiction(jurisdiction_db, "ET")
    judge = add_user(jurisdiction_db)
    add_member(jurisdiction_db, judge, et)
    old = interface_dictation.save_transcription(Transcription(title="Old IAC appeal"), judge.id, iac.id)

    with capture_telemetry() as telemetry:
        response = client_as(jurisdiction_app, jurisdiction_db, judge).get(f"/api/transcriptions/{old.id}")

    assert response.status_code == 404
    assert len(_blocked(telemetry)) == 1


def test_the_list_shows_only_the_jurisdiction_being_worked_in(jurisdiction_db, judge_in_two, jurisdiction_app):
    judge, *_ = judge_in_two
    client = client_as(jurisdiction_app, jurisdiction_db, judge)
    titles = [t["title"] for t in client.get("/api/transcriptions-metadata", headers={"X-Jurisdiction": "IAC"}).json()]
    assert titles == ["Smith v SSHD"]


# Anything else a page sends ------------------------------------------------------------------


def test_a_save_naming_another_jurisdictions_record_changes_nothing(jurisdiction_db, judge_in_two, jurisdiction_app):
    judge, _iac, _sscs, _in_iac, in_sscs = judge_in_two
    client = client_as(jurisdiction_app, jurisdiction_db, judge)

    with capture_telemetry() as telemetry:
        response = client.post(
            "/api/transcriptions", json={"id": str(in_sscs.id), "title": "renamed"}, headers={"X-Jurisdiction": "IAC"}
        )

    assert response.status_code == 404
    assert [(e["security.resource_type"], e["security.action"]) for e in _blocked(telemetry)] == [
        ("transcription", "write")
    ]
    with Session(jurisdiction_db) as session:
        assert session.get(Transcription, in_sscs.id).title == "PIP appeal"


def test_minutes_for_another_jurisdictions_record_are_refused(jurisdiction_db, judge_in_two, jurisdiction_app):
    judge, _iac, _sscs, _in_iac, in_sscs = judge_in_two
    body = {
        "transcription_id": str(in_sscs.id),
        "template": {"name": "General", "description": "d", "category": "common"},
        "action_type": "generate",
    }
    with capture_telemetry() as telemetry:
        response = client_as(jurisdiction_app, jurisdiction_db, judge).post(
            "/api/generate-or-edit-minutes", json=body, headers={"X-Jurisdiction": "IAC"}
        )
    assert response.status_code == 404
    assert [e["security.resource_type"] for e in _blocked(telemetry)] == ["minute_version"]


def test_an_upload_naming_another_jurisdictions_record_is_refused(jurisdiction_db, judge_in_two, jurisdiction_app):
    judge, _iac, _sscs, _in_iac, in_sscs = judge_in_two
    key = f"user-uploads/{judge.azure_user_id}/{in_sscs.id}.mp3"
    with capture_telemetry() as telemetry:
        response = client_as(jurisdiction_app, jurisdiction_db, judge).put(
            "/api/upload-audio", params={"key": key}, content=b"audio", headers={"X-Jurisdiction": "IAC"}
        )
    assert response.status_code == 404
    assert [e["security.resource_type"] for e in _blocked(telemetry)] == ["audio"]


def test_a_jurisdiction_the_user_does_not_belong_to_cannot_be_named(jurisdiction_db, judge_in_two, jurisdiction_app):
    judge, *_ = judge_in_two
    add_jurisdiction(jurisdiction_db, "ET")
    response = client_as(jurisdiction_app, jurisdiction_db, judge).get(
        "/api/transcriptions-metadata", headers={"X-Jurisdiction": "ET"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "jurisdiction_not_available"


# Administrators ------------------------------------------------------------------------------


@pytest.fixture
def admin_app():
    from transcribe_api.api.routes_dictation_privileged import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


def test_an_administrator_sees_only_the_jurisdiction_they_work_in(jurisdiction_db, judge_in_two, admin_app):
    _judge, iac, _sscs, in_iac, in_sscs = judge_in_two
    admin = add_user(jurisdiction_db, roles=("SystemAdministrator",))
    add_member(jurisdiction_db, admin, iac, role="admin")
    client = client_as(admin_app, jurisdiction_db, admin)

    listed = client.get("/api/admin/transcriptions").json()
    with capture_telemetry() as telemetry:
        other = client.get(f"/api/admin/transcriptions/{in_sscs.id}")

    assert [t["id"] for t in listed["items"]] == [str(in_iac.id)]
    assert other.status_code == 404
    assert client.get(f"/api/admin/transcriptions/{in_iac.id}").status_code == 200
    assert [e["security.resource_type"] for e in _blocked(telemetry)] == ["transcription"]


# Hearing recordings --------------------------------------------------------------------------


@pytest.fixture
def recording_app(jurisdiction_db):
    from transcribe_api.api.routes_recording import router
    from transcribe_api.runtime.db_recording import get_session

    app = FastAPI()
    app.include_router(router)

    def session():
        with Session(jurisdiction_db) as s:
            yield s

    app.dependency_overrides[get_session] = session
    return app


def _recording_client(app, user) -> TestClient:
    from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
    from transcribe_api.domain.auth.dependencies_recording import get_current_user

    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        db_user=user, app_roles=list(user.__dict__.get("app_roles", ["Judge"]))
    )
    return TestClient(app)


def _job(engine, user, jurisdiction) -> SpeechBatchJob:
    with Session(engine) as session:
        job = SpeechBatchJob(
            user_id=user.id,
            jurisdiction_id=jurisdiction.id,
            audio_url="https://blob/a.wav",
            status=JobStatus.SUCCEEDED,
            created_datetime=datetime.now(UTC),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def test_a_recording_of_another_jurisdiction_finds_nothing(jurisdiction_db, judge_in_two, recording_app):
    judge, iac, sscs, *_ = judge_in_two
    in_sscs = _job(jurisdiction_db, judge, sscs)
    in_iac = _job(jurisdiction_db, judge, iac)
    client = _recording_client(recording_app, judge)

    with capture_telemetry() as telemetry:
        other = client.get(f"/api/v1/jobs/{in_sscs.id}", headers={"X-Jurisdiction": "IAC"})
        audio = client.get(f"/api/v1/jobs/{in_sscs.id}/audio", headers={"X-Jurisdiction": "IAC"})
    listed = client.get("/api/v1/jobs", headers={"X-Jurisdiction": "IAC"}).json()

    assert (other.status_code, audio.status_code) == (404, 404)
    assert [(e["security.resource_type"], e["security.action"]) for e in _blocked(telemetry)] == [
        ("transcript", "read"),  # the resource the route's ownership check names
        ("audio", "read"),
    ]
    assert [j["job_id"] for j in listed["jobs"]] == [str(in_iac.id)]


def test_a_jurisdiction_without_hearing_recordings_has_no_recordings_area(jurisdiction_db, recording_app):
    et = add_jurisdiction(jurisdiction_db, "ET")
    judge = add_user(jurisdiction_db)
    add_member(jurisdiction_db, judge, et)
    client = _recording_client(recording_app, judge)

    assert client.get("/api/v1/jobs").status_code == 404
    assert client.get(f"/api/v1/jobs/{uuid4()}").status_code == 404


# No route is left out -------------------------------------------------------------------------


def _dependency_names(route) -> set[str]:
    def walk(dependant):
        for dependency in dependant.dependencies:
            yield getattr(dependency.call, "__qualname__", "")
            yield from walk(dependency)

    return set(walk(route.dependant))


def test_every_route_naming_a_transcription_checks_its_jurisdiction():
    """A new route that names a transcription and forgets the check fails here, rather
    than silently serving another jurisdiction's material."""
    from fastapi.routing import APIRoute

    import transcribe_api.api.routes_dictation as dictation

    naming = [
        r
        for r in dictation.router.routes
        if isinstance(r, APIRoute) and ("{transcription_id}" in r.path or r.path == "/download")
    ]
    assert len(naming) >= 14
    for route in naming:
        assert "_in_jurisdiction.<locals>.check" in _dependency_names(route), (route.methods, route.path)


def test_every_route_naming_a_recording_job_checks_its_jurisdiction():
    from fastapi.routing import APIRoute

    import transcribe_api.api.routes_recording as recording

    naming = [r for r in recording.router.routes if isinstance(r, APIRoute) and "{job_id}" in r.path]
    assert len(naming) >= 9
    for route in naming:
        assert "_job_in_jurisdiction.<locals>.check" in _dependency_names(route), (route.methods, route.path)


# Edge cases ---------------------------------------------------------------------------------


def test_a_caller_without_a_role_is_refused_by_the_role_check_not_recorded(
    jurisdiction_db, judge_in_two, jurisdiction_app
):
    judge, _iac, _sscs, _in_iac, in_sscs = judge_in_two
    judge.__dict__["app_roles"] = []
    with capture_telemetry() as telemetry:
        response = client_as(jurisdiction_app, jurisdiction_db, judge).get(
            f"/api/transcriptions/{in_sscs.id}", headers={"X-Jurisdiction": "IAC"}
        )
    assert response.status_code == 403
    assert _blocked(telemetry) == []


def test_a_blocked_upload_is_a_security_event_not_a_malformed_key(jurisdiction_db, judge_in_two, jurisdiction_app):
    judge, _iac, _sscs, _in_iac, in_sscs = judge_in_two
    key = f"user-uploads/{judge.azure_user_id}/{in_sscs.id}.mp3"
    with capture_telemetry() as telemetry:
        client_as(jurisdiction_app, jurisdiction_db, judge).put(
            "/api/upload-audio", params={"key": key}, content=b"audio", headers={"X-Jurisdiction": "IAC"}
        )
    assert telemetry.events("upload.rejected") == []
    assert len(_blocked(telemetry)) == 1


def test_an_idempotency_key_from_another_jurisdiction_does_not_return_its_job(
    jurisdiction_db, judge_in_two, recording_app
):
    judge, _iac, sscs, *_ = judge_in_two
    in_sscs = _job(jurisdiction_db, judge, sscs)
    with Session(jurisdiction_db) as session:
        row = session.get(SpeechBatchJob, in_sscs.id)
        row.idempotency_key = "same-key"
        session.add(row)
        session.commit()

    with capture_telemetry() as telemetry:
        response = _recording_client(recording_app, judge).post(
            "/api/v1/jobs",
            json={"audio_url": "https://blob/b.wav", "idempotency_key": "same-key"},
            headers={"X-Jurisdiction": "IAC"},
        )

    assert response.status_code == 409
    assert str(in_sscs.id) not in response.text
    assert [(e["security.resource_type"], e["security.action"]) for e in _blocked(telemetry)] == [("job", "submit")]
