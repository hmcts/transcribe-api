"""DIAAT-285: security.ownership_violation, the event behind the Security alert.

Blocked attempts (an ownership check refused the request) and detected access (the
post-authorisation assertion found another user's record about to be returned) both
record the event, with controlled values only, and the response is unchanged: the
same 404 as for an unknown ID.
"""

import logging
import os
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from hmcts_otel import conventions as c
from hmcts_otel import default_schema
from hmcts_otel.fastapi import RequestIdMiddleware
from hmcts_otel.testing import capture_telemetry
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import transcribe_api.runtime.security_events as sec
from transcribe_api.domain.exceptions import OwnershipConflictError
from transcribe_api.domain.models_dictation import MinuteVersion, Transcription, TranscriptionJob, User
from tests.unit.dictation.unit.api.test_routes import (
    TEST_OID,
    _make_transcription,
    _make_user,
    _make_user_with_oid,
    _mock_request,
)

TITLE = "Smith v Secretary of State, PA/50123/2024"
EVENT_KEYS = {sec.ATTR_RESOURCE_TYPE, sec.ATTR_ACTION, sec.ATTR_OUTCOME}
# What hmcts-otel adds to every event: name, class, source, journey, request ID.
ENVELOPE_KEYS = {c.CUSTOM_EVENT_NAME, c.EVENT_CLASS, c.EVENT_SOURCE, c.REQUEST_ID, c.JOURNEY_TYPE, c.JOURNEY_ID}


@pytest.fixture
def app():
    import transcribe_api.api.routes_dictation

    test_app = FastAPI()
    test_app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    test_app.add_middleware(RequestIdMiddleware)  # as main.py: events carry hmcts.request.id
    return test_app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def db(monkeypatch):
    """The real database functions on in-memory SQLite (as test_ownership_guard.py).

    Imported here, not at module level: importing them reads the settings, which the
    autouse environment fixture provides only once a test runs.
    """
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


def _violations(telemetry):
    return telemetry.events(sec.EVENT_OWNERSHIP_VIOLATION)


def _assert_clean(event, *identifiers):
    """Only the three controlled attributes and the envelope; no identifier anywhere."""
    assert set(event) - ENVELOPE_KEYS == EVENT_KEYS
    text = repr(event)
    for identifier in identifiers:
        assert str(identifier) not in text


# The event --------------------------------------------------------------------------


def test_the_event_is_registered_with_controlled_attributes_only():
    spec = default_schema.get_event(sec.EVENT_OWNERSHIP_VIOLATION)
    assert set(spec.attributes) == EVENT_KEYS
    assert default_schema.get_attribute(sec.ATTR_OUTCOME).allowed_values == {"blocked", "detected", "test"}
    assert default_schema.get_attribute(sec.ATTR_ACTION).allowed_values == {
        "read",
        "write",
        "delete",
        "reassign",
        "submit",
    }


def test_an_uncontrolled_value_is_dropped():
    with capture_telemetry() as telemetry:
        sec.record_ownership_violation(TITLE, sec.READ)
        (event,) = _violations(telemetry)
        assert sec.ATTR_RESOURCE_TYPE not in event
        telemetry.assert_no_prohibited_content(TITLE)


def test_the_assertion_passes_for_the_owner_and_records_nothing():
    user_id = uuid.uuid4()
    with capture_telemetry() as telemetry:
        assert sec.owner_matches(user_id, str(user_id), resource_type=sec.TRANSCRIPTION)
        assert _violations(telemetry) == []


@pytest.mark.parametrize("owner", [uuid.uuid4(), None])
def test_the_assertion_fails_for_another_owner_and_logs_the_type_only(owner, caplog):
    user_id = uuid.uuid4()
    with capture_telemetry() as telemetry, caplog.at_level(logging.ERROR, logger=sec.__name__):
        assert not sec.owner_matches(owner, user_id, resource_type=sec.DOCUMENT)
        (event,) = _violations(telemetry)
        assert event[sec.ATTR_OUTCOME] == "detected"
        assert event[sec.ATTR_RESOURCE_TYPE] == "document"
        _assert_clean(event, user_id, owner)
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "OWNERSHIP_ASSERTION_FAILED" in record.getMessage()
    assert str(user_id) not in record.getMessage()


# Detected: the post-authorisation assertion on the main read paths -------------------

READ_PATHS = {
    "transcription": "/api/transcriptions/{id}",
    "document-download": "/api/transcriptions/{id}/document-download",
    "download": "/api/download?transcription_id={id}",
    "sectioned-transcript": "/api/transcriptions/{id}/sectioned-transcript",
}
EXPECTED_TYPE = {
    "transcription": "transcription",
    "document-download": "document",
    "download": "document",
    "sectioned-transcript": "transcription",
}


@pytest.mark.parametrize("path", sorted(READ_PATHS))
def test_another_users_record_from_the_lookup_is_refused_as_not_found(client, as_user, mocker, path):
    user = _make_user_with_oid()
    as_user(user)
    owner = uuid.uuid4()
    transcription = _make_transcription(owner)
    transcription.title = TITLE
    url = READ_PATHS[path].format(id=transcription.id)

    # The answer for an unknown ID, for comparison.
    mocker.patch(
        "transcribe_api.api.routes_dictation.get_transcription_by_id",
        side_effect=HTTPException(status_code=404, detail="Transcription not found"),
    )
    not_found = client.get(url)

    # A broken lookup returns another user's record: refused the same way, and detected.
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    blob_manager = mocker.patch("transcribe_api.api.routes_dictation.AsyncAzureBlobManager")
    download_url = mocker.patch("transcribe_api.api.routes_dictation.generate_blob_download_url")
    with capture_telemetry() as telemetry:
        response = client.get(url)
        (event,) = _violations(telemetry)
        assert event[sec.ATTR_OUTCOME] == "detected"
        assert event[sec.ATTR_ACTION] == "read"
        assert event[sec.ATTR_RESOURCE_TYPE] == EXPECTED_TYPE[path]
        assert event[c.REQUEST_ID] == response.headers["x-request-id"]
        _assert_clean(event, user.id, owner, transcription.id, user.email)
        telemetry.assert_no_prohibited_content(TITLE, user.email)
    assert (
        (response.status_code, response.json())
        == (not_found.status_code, not_found.json())
        == (
            404,
            {"detail": "Transcription not found"},
        )
    )
    blob_manager.assert_not_called()
    download_url.assert_not_called()


def test_the_owners_read_records_nothing(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription = _make_transcription(user.id)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    with capture_telemetry() as telemetry:
        assert client.get(f"/api/transcriptions/{transcription.id}").status_code == 200
        assert _violations(telemetry) == []


# Blocked: ownership checks that refuse the request -------------------------------------


def test_an_upload_naming_another_users_transcription_is_recorded(client, as_user, mocker):
    user = _make_user_with_oid()
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_owner", return_value=uuid.uuid4())
    victim = uuid.uuid4()
    with capture_telemetry() as telemetry:
        response = client.put(
            "/api/upload-audio", params={"key": f"user-uploads/{TEST_OID}/{victim}.mp3"}, content=b"a"
        )
        assert response.status_code == 403
        (event,) = _violations(telemetry)
        assert (event[sec.ATTR_RESOURCE_TYPE], event[sec.ATTR_ACTION], event[sec.ATTR_OUTCOME]) == (
            "transcription",
            "write",
            "blocked",
        )
        _assert_clean(event, user.id, victim)


def test_a_blob_of_another_user_is_recorded_with_its_type():
    from transcribe_api.api.routes_dictation import _validate_blob_ownership

    user = _make_user_with_oid()
    other = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
    with capture_telemetry() as telemetry:
        with pytest.raises(HTTPException) as refused:
            _validate_blob_ownership(f"user-uploads/{other}/a.wav", user, _mock_request(), resource_type=sec.AUDIO)
        assert refused.value.status_code == 403
        (event,) = _violations(telemetry)
        assert (event[sec.ATTR_RESOURCE_TYPE], event[sec.ATTR_OUTCOME]) == ("audio", "blocked")
        _assert_clean(event, user.id, other)
        # The caller's own blob records nothing.
        _validate_blob_ownership(f"user-uploads/{TEST_OID}/a.wav", user, _mock_request())
        assert len(_violations(telemetry)) == 1


def test_an_ownership_conflict_is_recorded_where_it_is_raised(db):
    victim, attacker = _user(db), _user(db)
    victims = db.save_transcription(Transcription(title=TITLE), victim.id)
    job = db.save_transcription_job(TranscriptionJob(transcription_id=victims.id, dialogue_entries=[]))
    attackers = db.save_transcription(Transcription(), attacker.id)
    with capture_telemetry() as telemetry:
        with pytest.raises(OwnershipConflictError):
            db.save_transcription(Transcription(id=victims.id), attacker.id)  # POST /api/transcriptions
        with pytest.raises(OwnershipConflictError):
            db.load_or_create_transcription(victims.id, attacker.id)  # upload poller
        with pytest.raises(OwnershipConflictError):
            db.save_transcription_job(TranscriptionJob(id=job.id, transcription_id=attackers.id, dialogue_entries=[]))
        events = _violations(telemetry)
        assert [(e[sec.ATTR_RESOURCE_TYPE], e[sec.ATTR_ACTION], e[sec.ATTR_OUTCOME]) for e in events] == [
            ("transcription", "reassign", "blocked"),
            ("transcription", "reassign", "blocked"),
            ("transcription_job", "reassign", "blocked"),
        ]
        for event in events:
            _assert_clean(event, victim.id, attacker.id, victims.id, job.id)
        telemetry.assert_no_prohibited_content(TITLE, victim.email, attacker.email)
        # The owner's own saves record nothing.
        db.save_transcription(Transcription(id=victims.id, title="renamed"), victim.id)
        db.load_or_create_transcription(victims.id, victim.id)
        assert len(_violations(telemetry)) == 3


def test_a_minute_version_conflict_has_its_own_type(db):
    victim, attacker = _user(db), _user(db)
    victims = db.save_transcription(Transcription(), victim.id)
    attackers = db.save_transcription(Transcription(), attacker.id)
    minutes = db.save_minute_version(MinuteVersion(transcription_id=victims.id, html_content="<p>v</p>"))
    with capture_telemetry() as telemetry:
        with pytest.raises(OwnershipConflictError):
            db.save_minute_version(MinuteVersion(id=minutes.id, transcription_id=attackers.id, html_content="x"))
        (event,) = _violations(telemetry)
        assert event[sec.ATTR_RESOURCE_TYPE] == "minute_version"


@pytest.mark.parametrize(
    ("call", "action"),
    [
        (lambda db, tid, uid: db.get_tags_for_transcription(tid, uid), "read"),
        (lambda db, tid, uid: db.add_tag_to_transcription(tid, "urgent", uid), "write"),
        (lambda db, tid, uid: db.remove_tag_from_transcription(tid, "urgent", uid), "write"),
    ],
)
def test_tags_of_another_users_transcription_are_not_found_and_recorded(db, call, action):
    victim, attacker = _user(db), _user(db)
    victims = db.save_transcription(Transcription(title=TITLE), victim.id)
    with capture_telemetry() as telemetry:
        with pytest.raises(HTTPException) as other_owner:
            call(db, victims.id, attacker.id)
        with pytest.raises(HTTPException) as unknown:
            call(db, uuid.uuid4(), attacker.id)
        # Same answer for "someone else's" and "no such transcription"; only the first is recorded.
        assert (other_owner.value.status_code, other_owner.value.detail) == (
            unknown.value.status_code,
            unknown.value.detail,
        )
        (event,) = _violations(telemetry)
        assert (event[sec.ATTR_ACTION], event[sec.ATTR_OUTCOME]) == (action, "blocked")
        _assert_clean(event, victim.id, attacker.id, victims.id)


# Controlled test of the Security alert route (AC2) ------------------------------------


def _admin(as_user):
    user = User(id=uuid.uuid4(), email="admin@justice.gov.uk", azure_user_id="admin-oid")
    user.__dict__["app_roles"] = ["SystemAdministrator"]
    as_user(user)
    return user


def _settings(mocker, *, enabled, environment: str | None = "test"):
    settings = MagicMock(SECURITY_ALERT_TEST_TRIGGER_ENABLED=enabled)
    mocker.patch("transcribe_api.api.routes_dictation.get_recording_settings", return_value=settings)
    # The guard reads ENVIRONMENT from the process, not the settings default.
    mocker.patch.dict("os.environ", {} if environment is None else {"ENVIRONMENT": environment})
    if environment is None:
        os.environ.pop("ENVIRONMENT", None)


def test_the_test_event_is_off_by_default(monkeypatch):
    from transcribe_api.runtime.settings_recording import Settings

    assert Settings.model_fields["SECURITY_ALERT_TEST_TRIGGER_ENABLED"].default is False
    monkeypatch.setenv("SECURITY_ALERT_TEST_TRIGGER_ENABLED", " ")
    assert Settings().SECURITY_ALERT_TEST_TRIGGER_ENABLED is False  # blank is off


@pytest.mark.parametrize(
    ("enabled", "environment"),
    [
        (False, "test"),
        (True, None),  # unset: fail closed, whatever the settings default says
        (True, "dev"),  # no CNP environment is called dev
        (True, "preview"),
        (True, "aat"),  # CNP's deployed environments are not on the allowlist
        (True, "perftest"),
        (True, "ithc"),
        (True, "demo"),
        (True, "prod"),
        (True, " PROD "),
        (True, "stg"),
        (True, ""),
    ],
)
def test_the_test_event_is_not_found_unless_enabled_in_an_allowed_environment(
    client, as_user, mocker, enabled, environment
):
    _admin(as_user)
    _settings(mocker, enabled=enabled, environment=environment)
    with capture_telemetry() as telemetry:
        assert client.post("/api/security/alert-test").status_code == 404
        assert _violations(telemetry) == []


def test_the_test_event_records_a_marked_synthetic_violation(client, as_user, mocker):
    user = _admin(as_user)
    _settings(mocker, enabled=True)
    with capture_telemetry() as telemetry:
        response = client.post("/api/security/alert-test")
        assert response.status_code == 202
        (event,) = _violations(telemetry)
        assert event[sec.ATTR_OUTCOME] == "test"
        assert event[c.REQUEST_ID] == response.headers["x-request-id"]
        _assert_clean(event, user.id, user.email)


def test_the_test_event_needs_the_administrator_role(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    _settings(mocker, enabled=True)
    with capture_telemetry() as telemetry:
        assert client.post("/api/security/alert-test").status_code == 403
        assert _violations(telemetry) == []
