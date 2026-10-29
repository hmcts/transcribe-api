"""DIAAT-286: audit records of sensitive actions, in the application database.

AC1: an investigator can tell whether an agreed sensitive action occurred (viewed,
exported, deleted, access denied) and whether it succeeded, from ``audit_log``: the
actor (internal user UUID), action, outcome, timestamp, object type and ID, and the
request ID that joins the identifier-free security telemetry. No content.

Writing an audit record never breaks the request (fail-open).
"""

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from hmcts_azure_auth.audit import AuditEvent
from hmcts_otel import conventions as c
from hmcts_otel import default_schema, request_id_context
from hmcts_otel.fastapi import RequestIdMiddleware
from hmcts_otel.testing import capture_telemetry
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import transcribe_api.runtime.security_events as sec
from transcribe_api.domain import audit
from transcribe_api.domain.models_dictation import Transcription, TranscriptionJob, User
from tests.unit.dictation.unit.api.test_routes import TEST_OID, _make_transcription, _make_user, _make_user_with_oid

TITLE = "Smith v Secretary of State, PA/50123/2024"
REQUEST_ID = "0f8e2b7c-3a4d-4e5f-9a8b-7c6d5e4f3a2b"


@pytest.fixture
def app():
    # Not api.privileged_routes: importing it before test_live_transcription_submission_logging
    # reloads utils.dependencies breaks test_privileged_routes' overrides. Its audit
    # tests are in test_privileged_routes.py.
    import transcribe_api.api.routes_dictation

    test_app = FastAPI()
    test_app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    test_app.add_middleware(RequestIdMiddleware)  # as main.py: hmcts.request.id on every request
    return test_app


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _row(audit_log):
    (row,) = audit_log.rows()  # one insert per audited request
    return row


def _assert_no_content(row, *values):
    text = repr(row.model_dump())
    for value in values:
        assert str(value) not in text


def _transcription_with_document(user_id, blob_path=f"user-uploads/{TEST_OID}/hearing-document.docx"):
    transcription = _make_transcription(user_id)
    transcription.title = TITLE
    job = TranscriptionJob(transcription_id=transcription.id, dialogue_entries=[], s3_audio_url=blob_path)
    transcription.__dict__["transcription_jobs"] = [job]
    return transcription, job


def _storage(mocker, *, status=200, error=None):
    """The blob storage HTTP client used by the streaming routes."""
    response = MagicMock(status_code=status, headers={})

    async def chunks(chunk_size):
        yield b"document bytes"

    response.aiter_bytes = chunks
    response.aclose = AsyncMock()
    http = MagicMock()
    http.send = AsyncMock(side_effect=error, return_value=response)
    http.aclose = AsyncMock()
    mocker.patch("transcribe_api.api.routes_dictation.httpx.AsyncClient", return_value=http)
    mocker.patch(
        "transcribe_api.api.routes_dictation.generate_blob_download_url",
        new=AsyncMock(return_value="https://blob/doc?sas"),
    )
    return http


# The writer ---------------------------------------------------------------------------


def test_a_record_has_the_actor_action_outcome_object_and_request(audit_log):
    actor, record = uuid.uuid4(), uuid.uuid4()
    with request_id_context(REQUEST_ID):
        assert audit.record_audit(
            audit.TRANSCRIPT_EXPORTED, actor_id=actor, outcome=audit.SUCCESS, object_type="document", object_id=record
        )
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.user_id, row.object_type, row.object_id, row.request_id) == (
        "transcript.export",
        "success",
        str(actor),
        "document",
        str(record),
        REQUEST_ID,
    )
    assert row.timestamp is not None
    assert row.email is None  # the actor is the internal user UUID only


def test_the_auth_librarys_denials_carry_the_request_id_and_outcome(audit_log):
    event = AuditEvent(event_type="ACCESS_DENIED", user_id="oid", email="x@justice.gov.uk", resource="GET /api/admin")
    with request_id_context(REQUEST_ID):
        audit.write_audit_event(event)
    row = _row(audit_log)
    assert (row.outcome, row.request_id, row.email) == ("denied", REQUEST_ID, "x@justice.gov.uk")


def test_a_failed_write_never_raises_logs_types_only_and_records_an_identifier_free_event(
    audit_log, monkeypatch, caplog
):
    actor, record = uuid.uuid4(), uuid.uuid4()

    def outage():
        message = f"could not connect for {actor}"
        raise ConnectionError(message)

    monkeypatch.setattr(audit, "get_engine", outage)
    with capture_telemetry() as telemetry, caplog.at_level(logging.ERROR, logger=audit.__name__):
        written = audit.record_audit(
            audit.TRANSCRIPT_VIEWED,
            actor_id=actor,
            outcome=audit.SUCCESS,
            object_type="transcription",
            object_id=record,
        )
        assert written is False
        (event,) = telemetry.events(audit.EVENT_WRITE_FAILED)
        assert event[audit.ATTR_ACTION] == "transcript.view"
        telemetry.assert_no_prohibited_content(str(actor), str(record))
    (log,) = [r for r in caplog.records if r.name == audit.__name__]
    assert log.getMessage() == "AUDIT_WRITE_FAILED event_type=transcript.view error=ConnectionError"


def test_the_write_failed_event_is_registered_without_identifiers():
    spec = default_schema.get_event(audit.EVENT_WRITE_FAILED)
    assert set(spec.attributes) == {audit.ATTR_ACTION, "hmcts.error.code"}
    assert audit.TRANSCRIPT_VIEWED in default_schema.get_attribute(audit.ATTR_ACTION).allowed_values


def test_a_request_succeeds_when_the_audit_database_is_down(client, as_user, mocker, monkeypatch):
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription = _make_transcription(user.id)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    monkeypatch.setattr(audit, "get_engine", MagicMock(side_effect=ConnectionError("down")))
    with capture_telemetry() as telemetry:
        assert client.get(f"/api/transcriptions/{transcription.id}").status_code == 200
        assert len(telemetry.events(audit.EVENT_WRITE_FAILED)) == 1


# Viewed ---------------------------------------------------------------------------------


def test_viewing_a_transcription_is_recorded(client, as_user, mocker, audit_log):
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription = _make_transcription(user.id)
    transcription.title = TITLE
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    response = client.get(f"/api/transcriptions/{transcription.id}")
    assert response.status_code == 200
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.user_id, row.object_type, row.object_id) == (
        "transcript.view",
        "success",
        str(user.id),
        "transcription",
        str(transcription.id),
    )
    assert row.request_id == response.headers["x-request-id"]
    _assert_no_content(row, TITLE, user.email)


def test_a_refused_lookup_is_recorded_as_denied(client, as_user, mocker, audit_log):
    """Unknown, or another user's: the same 404, and the same audit outcome."""
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch(
        "transcribe_api.api.routes_dictation.get_transcription_by_id",
        side_effect=HTTPException(status_code=404, detail="Transcription not found"),
    )
    target = uuid.uuid4()
    assert client.get(f"/api/transcriptions/{target}").status_code == 404
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.object_id) == ("transcript.view", "denied", str(target))


def test_a_detected_violation_is_one_denied_record_joined_to_the_alert_by_request_id(
    client, as_user, mocker, audit_log
):
    """DIAAT-285's event has no identifiers; the audit row, found by its request ID,
    says who tried to reach which record."""
    user = _make_user_with_oid()
    as_user(user)
    transcription = _make_transcription(uuid.uuid4())  # another user's, from a broken lookup
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    with capture_telemetry() as telemetry:
        response = client.get(f"/api/transcriptions/{transcription.id}")
        (event,) = telemetry.events(sec.EVENT_OWNERSHIP_VIOLATION)
        telemetry.assert_no_prohibited_content(str(user.id), str(transcription.id))
    assert response.status_code == 404
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.user_id, row.object_id) == (
        "transcript.view",
        "denied",
        str(user.id),
        str(transcription.id),
    )
    assert row.detail == {"ownership_violation": "detected"}
    assert row.request_id == event[c.REQUEST_ID] == response.headers["x-request-id"]


def test_reading_the_transcript_text_is_recorded_as_a_view(client, as_user, mocker, audit_log):
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription = _make_transcription(user.id)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_jobs", return_value=[])
    assert client.get(f"/api/transcriptions/{transcription.id}/jobs").status_code == 200
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.object_type) == ("transcript.view", "success", "transcript")


def test_a_sectioned_transcript_that_cannot_be_read_is_recorded_as_failed(client, as_user, mocker, audit_log):
    user = _make_user_with_oid()
    as_user(user)
    transcription = _make_transcription(user.id)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    mocker.patch("transcribe_api.api.routes_dictation.AsyncAzureBlobManager", side_effect=RuntimeError("storage down"))
    assert client.get(f"/api/transcriptions/{transcription.id}/sectioned-transcript").status_code == 500
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.object_type) == ("transcript.view", "failed", "sectioned_transcript")


# Exported -------------------------------------------------------------------------------


def test_issuing_a_document_link_is_recorded_as_an_export(client, as_user, mocker, audit_log):
    user = _make_user_with_oid()
    as_user(user)
    transcription, _ = _transcription_with_document(user.id)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    response = client.get(f"/api/transcriptions/{transcription.id}/document-download")
    assert response.status_code == 200
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.object_type, row.object_id, row.detail) == (
        "transcript.export",
        "success",
        "document",
        str(transcription.id),
        {"via": "link"},
    )
    _assert_no_content(row, TITLE, "hearing-document.docx")


def test_a_missing_document_after_the_access_decision_is_failed_not_denied(client, as_user, mocker, audit_log):
    user = _make_user_with_oid()
    as_user(user)
    transcription = _make_transcription(user.id)  # the caller's, but no document stored
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    assert client.get(f"/api/transcriptions/{transcription.id}/document-download").status_code == 404
    assert _row(audit_log).outcome == "failed"


def test_a_streamed_document_is_recorded_as_an_export(client, as_user, mocker, audit_log):
    user = _make_user_with_oid()
    as_user(user)
    transcription, _ = _transcription_with_document(user.id)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    _storage(mocker)
    response = client.get(f"/api/download?transcription_id={transcription.id}")
    assert response.status_code == 200
    assert response.content == b"document bytes"
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.detail) == ("transcript.export", "success", {"via": "stream"})


@pytest.mark.parametrize(
    ("storage", "status"),
    [({"status": 403}, 403), ({"error": RuntimeError("connection reset")}, 500)],
    ids=["storage-refused", "storage-unreachable"],
)
def test_a_download_whose_storage_read_fails_is_recorded_as_failed(client, as_user, mocker, audit_log, storage, status):
    user = _make_user_with_oid()
    as_user(user)
    transcription, _ = _transcription_with_document(user.id)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    _storage(mocker, **storage)
    assert client.get(f"/api/download?transcription_id={transcription.id}").status_code == status
    row = _row(audit_log)
    assert (row.event_type, row.outcome) == ("transcript.export", "failed")


def test_a_download_of_another_users_blob_is_one_denied_record(client, as_user, mocker, audit_log):
    user = _make_user_with_oid()
    as_user(user)
    other = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
    transcription, _ = _transcription_with_document(user.id, blob_path=f"user-uploads/{other}/doc.docx")
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    http = _storage(mocker)
    assert client.get(f"/api/download?transcription_id={transcription.id}").status_code == 403
    http.send.assert_not_called()
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.object_id, row.detail) == (
        "transcript.export",
        "denied",
        str(transcription.id),
        {"via": "stream", "ownership_violation": "blocked"},
    )


@pytest.mark.parametrize(
    ("range_header", "recorded"),
    [(None, True), ("bytes=0-", True), ("bytes=1048576-", False)],
    ids=["whole-file", "playback-start", "seek"],
)
def test_audio_playback_is_recorded_once_not_per_seek(client, as_user, mocker, audit_log, range_header, recorded):
    user = _make_user_with_oid()
    as_user(user)
    transcription, job = _transcription_with_document(user.id, blob_path=f"user-uploads/{TEST_OID}/a.wav")
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_jobs", return_value=[job])
    _storage(mocker, status=206 if range_header else 200)
    headers = {"Range": range_header} if range_header else {}
    url = f"/api/transcriptions/{transcription.id}/jobs/{job.id}/audio"
    assert client.get(url, headers=headers).status_code in {200, 206}
    rows = audit_log.rows()
    assert len(rows) == (1 if recorded else 0)
    if recorded:
        assert (rows[0].event_type, rows[0].object_type, rows[0].detail) == (
            "transcript.export",
            "audio",
            {"via": "stream", "job_id": str(job.id)},
        )


def test_a_refused_seek_is_still_recorded(client, as_user, mocker, audit_log):
    as_user(_make_user_with_oid())
    mocker.patch(
        "transcribe_api.api.routes_dictation.get_transcription_by_id",
        side_effect=HTTPException(status_code=404, detail="Transcription not found"),
    )
    url = f"/api/transcriptions/{uuid.uuid4()}/jobs/{uuid.uuid4()}/audio"
    assert client.get(url, headers={"Range": "bytes=500-"}).status_code == 404
    assert _row(audit_log).outcome == "denied"


# Deleted --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("effect", "status", "outcome"),
    [
        (None, 204, "success"),
        (HTTPException(status_code=404, detail="Transcription not found"), 404, "denied"),
        (RuntimeError("database down"), 500, "failed"),
    ],
    ids=["deleted", "refused", "error"],
)
def test_deleting_is_recorded_with_its_outcome(client, as_user, mocker, audit_log, effect, status, outcome):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.delete_transcription_by_id", side_effect=effect)
    target = uuid.uuid4()
    assert client.delete(f"/api/transcriptions/{target}").status_code == status
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.user_id, row.object_id) == (
        "transcript.delete",
        outcome,
        str(user.id),
        str(target),
    )


# Access denied outside an audited action: the DIAAT-285 call sites -------------------------


@pytest.fixture
def db(monkeypatch):
    """The real database functions on in-memory SQLite (as test_security_events.py)."""
    import tests.unit.dictation.unit.app.database.test_ownership_guard  # noqa: F401 - registers JSONB as JSON on SQLite
    from transcribe_api.domain import interface_dictation as interface_functions

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(interface_functions, "engine", engine)
    return interface_functions


def _user(db) -> User:
    with Session(db.engine) as session:
        user = User(email=f"{uuid.uuid4()}@example.com", azure_user_id=str(uuid.uuid4()))
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def test_an_upload_naming_another_users_transcription_is_an_access_denied_record(client, as_user, mocker, audit_log):
    user = _make_user_with_oid()
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_owner", return_value=uuid.uuid4())
    victim = uuid.uuid4()
    response = client.put("/api/upload-audio", params={"key": f"user-uploads/{TEST_OID}/{victim}.mp3"}, content=b"a")
    assert response.status_code == 403
    row = _row(audit_log)
    assert (row.event_type, row.outcome, row.user_id, row.object_type, row.object_id, row.detail) == (
        "ACCESS_DENIED",
        "denied",
        str(user.id),
        "transcription",
        str(victim),
        {"action": "write", "ownership_violation": "blocked"},
    )
    assert row.request_id == response.headers["x-request-id"]


def test_an_upload_into_another_users_folder_names_the_transcription_in_the_key(client, as_user, audit_log):
    user = _make_user_with_oid()
    as_user(user)
    other, target = "b2c3d4e5-f6a7-8901-bcde-f12345678901", uuid.uuid4()
    response = client.put("/api/upload-audio", params={"key": f"user-uploads/{other}/{target}.mp3"}, content=b"a")
    assert response.status_code == 403
    row = _row(audit_log)
    assert (row.event_type, row.user_id, row.object_type, row.object_id, row.detail) == (
        "ACCESS_DENIED",
        str(user.id),
        "audio",
        str(target),
        {"action": "write", "ownership_violation": "blocked"},
    )
    _assert_no_content(row, other)  # the blob path (an owner ID) is not recorded


def test_ownership_refusals_in_the_database_layer_name_the_actor_and_record(db, audit_log):
    victim, attacker = _user(db), _user(db)
    victims = db.save_transcription(Transcription(title=TITLE), victim.id)
    with pytest.raises(HTTPException):
        db.get_tags_for_transcription(victims.id, attacker.id)
    with pytest.raises(Exception, match="Transcription"):
        db.save_transcription(Transcription(id=victims.id), attacker.id)
    with pytest.raises(Exception, match="Transcription"):
        db.load_or_create_transcription(victims.id, attacker.id)
    rows = audit_log.rows()
    assert [(r.event_type, r.user_id, r.object_id, r.detail["action"]) for r in rows] == [
        ("ACCESS_DENIED", str(attacker.id), str(victims.id), "read"),
        ("ACCESS_DENIED", str(attacker.id), str(victims.id), "reassign"),
        ("ACCESS_DENIED", str(attacker.id), str(victims.id), "reassign"),
    ]
    for row in rows:
        _assert_no_content(row, TITLE, victim.email, attacker.email)


def test_a_minute_version_conflict_from_the_route_names_the_actor(db, audit_log):
    victim, attacker = _user(db), _user(db)
    victims = db.save_transcription(Transcription(), victim.id)
    attackers = db.save_transcription(Transcription(), attacker.id)
    from transcribe_api.domain.models_dictation import MinuteVersion

    minutes = db.save_minute_version(MinuteVersion(transcription_id=victims.id, html_content="<p>v</p>"))
    with pytest.raises(Exception, match="MinuteVersion"):
        db.save_minute_version(
            MinuteVersion(id=minutes.id, transcription_id=attackers.id, html_content="x"), actor_id=attacker.id
        )
    row = _row(audit_log)
    assert (row.user_id, row.object_type, row.object_id) == (str(attacker.id), "minute_version", str(minutes.id))


def test_the_alert_test_event_writes_no_audit_record(audit_log):
    sec.record_ownership_violation(sec.TRANSCRIPTION, sec.READ, sec.TEST, actor_id=uuid.uuid4())
    assert audit_log.rows() == []
