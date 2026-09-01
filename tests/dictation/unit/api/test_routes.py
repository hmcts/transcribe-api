"""Route-level tests for api/routes.py.

Auth is mocked via dependency_overrides — business logic and status codes are
what's under test here, not the auth stack (that's covered in test_dependencies.py).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from transcribe_api.domain.models_dictation import User


def _make_user(roles: list[str] | None = None, completed_onboarding: bool = False) -> User:
    user = User(id=uuid.uuid4(), email="test@justice.gov.uk", azure_user_id="test-oid")
    user.__dict__["app_roles"] = roles or []
    user.has_completed_onboarding = completed_onboarding
    return user


@pytest.fixture
def app():
    from fastapi import FastAPI

    import transcribe_api.api.routes_dictation
    test_app = FastAPI()
    test_app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    return test_app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def as_user(app):
    """Inject a User into get_current_user for the duration of a test."""
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user
    yield lambda user: app.dependency_overrides.__setitem__(get_current_user, lambda: user)
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Health endpoints — no auth required
# ---------------------------------------------------------------------------

def test_health_returns_ok(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_healthcheck_legacy_returns_ok(client):
    body = client.get("/api/healthcheck").json()
    assert body["status"] == "ok"
    assert "version" in body


def test_health_returns_app_version_from_env(monkeypatch, client):
    """APP_VERSION env var flows end-to-end into /api/health and /api/healthcheck.

    Reload order matters: APP_VERSION is defined in `utils.version` and re-exported
    by `api.routes`, so the leaf module must be reloaded first.
    """
    import importlib

    import transcribe_api.api.routes_dictation
    import transcribe_api.runtime.version

    monkeypatch.setenv("APP_VERSION", "1.2.3-abc1234")
    importlib.reload(transcribe_api.runtime.version)
    importlib.reload(transcribe_api.api.routes_dictation)
    try:
        assert client.get("/api/health").json() == {"status": "ok", "version": "1.2.3-abc1234"}
        assert client.get("/api/healthcheck").json() == {"status": "ok", "version": "1.2.3-abc1234"}
    finally:
        importlib.reload(transcribe_api.runtime.version)
        importlib.reload(transcribe_api.api.routes_dictation)


# ---------------------------------------------------------------------------
# GET /api/user/onboarding-status
# ---------------------------------------------------------------------------

def test_onboarding_status_is_allowlisted_when_user_has_valid_role(client, as_user):
    as_user(_make_user(roles=["Judge"]))
    body = client.get("/api/user/onboarding-status").json()
    assert body["is_allowlisted"] is True


def test_onboarding_status_not_allowlisted_when_user_has_no_roles(client, as_user):
    as_user(_make_user(roles=[]))
    body = client.get("/api/user/onboarding-status").json()
    assert body["is_allowlisted"] is False
    assert body["should_show_coming_soon"] is True


def test_onboarding_status_reflects_completion_state(client, as_user):
    as_user(_make_user(roles=["Judge"], completed_onboarding=True))
    body = client.get("/api/user/onboarding-status").json()
    assert body["has_completed_onboarding"] is True
    assert body["should_show_onboarding"] is False


def test_onboarding_status_incomplete_user_should_show_onboarding(client, as_user):
    as_user(_make_user(roles=["Judge"], completed_onboarding=False))
    body = client.get("/api/user/onboarding-status").json()
    assert body["should_show_onboarding"] is True


# ---------------------------------------------------------------------------
# POST /api/user/reset-onboarding — forbidden outside local/dev
# ---------------------------------------------------------------------------

def test_reset_onboarding_returns_403_in_test_environment(client, as_user):
    # ENVIRONMENT is "test" (set by conftest setup_test_environment)
    as_user(_make_user(roles=["SystemAdministrator"]))
    response = client.post("/api/user/reset-onboarding")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/document-content — any authenticated user
# ---------------------------------------------------------------------------

def test_document_content_returns_200_for_authenticated_user(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    mocker.patch(
        "transcribe_api.api.routes_dictation.get_document_content",
        return_value={"hearing_types": [], "legal_frameworks": [], "anonymity": {}},
    )
    response = client.get("/api/document-content")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# GET /api/templates
# ---------------------------------------------------------------------------

def test_get_templates_returns_list(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    mocker.patch("transcribe_api.api.routes_dictation.get_all_templates", return_value=[])
    response = client.get("/api/templates")
    assert response.status_code == 200
    assert response.json() == {"templates": []}


# ---------------------------------------------------------------------------
# GET /api/transcriptions-metadata
# ---------------------------------------------------------------------------

def test_transcriptions_metadata_returns_empty_list(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    mocker.patch("transcribe_api.api.routes_dictation.fetch_transcriptions_metadata", return_value=[])
    response = client.get("/api/transcriptions-metadata")
    assert response.status_code == 200
    assert response.json() == []


def test_transcriptions_metadata_accepts_timezone_param(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    mocker.patch("transcribe_api.api.routes_dictation.fetch_transcriptions_metadata", return_value=[])
    response = client.get("/api/transcriptions-metadata?timezone=America/New_York")
    assert response.status_code == 200


def test_transcriptions_metadata_falls_back_on_invalid_timezone(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    mocker.patch("transcribe_api.api.routes_dictation.fetch_transcriptions_metadata", return_value=[])
    response = client.get("/api/transcriptions-metadata?timezone=Not/ATimezone")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# PUT /api/upload-audio — key validation (no Azure calls needed for these)
# ---------------------------------------------------------------------------

def test_upload_audio_rejects_key_without_correct_prefix(client, as_user):
    as_user(_make_user(roles=["Judge"]))
    response = client.put("/api/upload-audio?key=bad-prefix/file.wav", content=b"data")
    assert response.status_code == 400


def test_upload_audio_rejects_key_with_mismatched_email(client, as_user):
    as_user(_make_user(roles=["Judge"]))
    response = client.put(
        "/api/upload-audio?key=user-uploads/other@example.com/file.wav",
        content=b"data",
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/user  (user profile endpoints)
# ---------------------------------------------------------------------------

def test_get_current_user_returns_user(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.get_user_by_id", return_value=user)
    response = client.get("/api/user")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# _validate_blob_ownership — unit tests for the blob path helper
# ---------------------------------------------------------------------------

TEST_OID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
OTHER_OID = "b2c3d4e5-f6a7-8901-bcde-f12345678901"


def _make_user_with_oid(oid: str = TEST_OID, email: str = "judge@justice.gov.uk") -> User:
    user = User(id=uuid.uuid4(), email=email, azure_user_id=oid)
    user.__dict__["app_roles"] = ["Judge"]
    return user


def _mock_request(method: str = "GET", path: str = "/api/download") -> MagicMock:
    req = MagicMock()
    req.method = method
    req.url.path = path
    return req


def test_validate_blob_ownership_passes_for_matching_oid():
    from transcribe_api.api.routes_dictation import _validate_blob_ownership
    user = _make_user_with_oid(TEST_OID)
    _validate_blob_ownership(f"user-uploads/{TEST_OID}/audio.wav", user, _mock_request())


def test_validate_blob_ownership_raises_403_for_mismatched_oid():
    from transcribe_api.api.routes_dictation import _validate_blob_ownership
    user = _make_user_with_oid(TEST_OID)
    with pytest.raises(HTTPException) as exc_info:
        _validate_blob_ownership(f"user-uploads/{OTHER_OID}/audio.wav", user, _mock_request())
    assert exc_info.value.status_code == 403


def test_validate_blob_ownership_passes_for_matching_legacy_email():
    from transcribe_api.api.routes_dictation import _validate_blob_ownership
    user = _make_user_with_oid(email="judge@justice.gov.uk")
    _validate_blob_ownership("user-uploads/judge@justice.gov.uk/audio.wav", user, _mock_request())


def test_validate_blob_ownership_passes_for_legacy_email_case_insensitive():
    from transcribe_api.api.routes_dictation import _validate_blob_ownership
    user = _make_user_with_oid(email="Judge@Justice.Gov.UK")
    _validate_blob_ownership("user-uploads/judge@justice.gov.uk/audio.wav", user, _mock_request())


def test_validate_blob_ownership_raises_403_for_mismatched_legacy_email():
    from transcribe_api.api.routes_dictation import _validate_blob_ownership
    user = _make_user_with_oid(email="judge@justice.gov.uk")
    with pytest.raises(HTTPException) as exc_info:
        _validate_blob_ownership("user-uploads/other@justice.gov.uk/audio.wav", user, _mock_request())
    assert exc_info.value.status_code == 403


def test_validate_blob_ownership_passes_for_non_user_uploads_path():
    from transcribe_api.api.routes_dictation import _validate_blob_ownership
    user = _make_user_with_oid()
    _validate_blob_ownership("other-container/anything/file.wav", user, _mock_request())


def test_validate_blob_ownership_logs_unauthorised_attempt_on_oid_mismatch(mocker):
    from transcribe_api.api.routes_dictation import _validate_blob_ownership
    mock_logger = mocker.patch("transcribe_api.api.routes_dictation.logger")
    user = _make_user_with_oid(TEST_OID)
    with pytest.raises(HTTPException):
        _validate_blob_ownership(f"user-uploads/{OTHER_OID}/audio.wav", user, _mock_request("GET", "/api/download"))
    log_call = mock_logger.warning.call_args[0][0]
    assert "UNAUTHORISED_ACCESS_ATTEMPT" in log_call
    assert "blob_ownership_mismatch" in mock_logger.warning.call_args[0][0]


def test_validate_blob_ownership_logs_unauthorised_attempt_on_email_mismatch(mocker):
    from transcribe_api.api.routes_dictation import _validate_blob_ownership
    mock_logger = mocker.patch("transcribe_api.api.routes_dictation.logger")
    user = _make_user_with_oid(email="judge@justice.gov.uk")
    with pytest.raises(HTTPException):
        _validate_blob_ownership("user-uploads/other@justice.gov.uk/audio.wav", user, _mock_request("GET", "/api/audio"))
    log_call = mock_logger.warning.call_args[0][0]
    assert "UNAUTHORISED_ACCESS_ATTEMPT" in log_call


@pytest.mark.parametrize("bad_path", ["user-uploads/", "user-uploads//file.wav"])
def test_validate_blob_ownership_raises_400_for_malformed_path(bad_path):
    from transcribe_api.api.routes_dictation import _validate_blob_ownership
    user = _make_user_with_oid()
    with pytest.raises(HTTPException) as exc_info:
        _validate_blob_ownership(bad_path, user, _mock_request())
    assert exc_info.value.status_code == 400


def test_upload_audio_rejects_key_with_mismatched_oid(client, as_user):
    user = _make_user_with_oid(TEST_OID)
    as_user(user)
    response = client.put(
        f"/api/upload-audio?key=user-uploads/{OTHER_OID}/file.wav",
        content=b"data",
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Audit log events — ST-J3
# ---------------------------------------------------------------------------

def _make_transcription(user_id=None) -> "Transcription":
    from transcribe_api.domain.models_dictation import Transcription
    t = Transcription(id=uuid.uuid4(), user_id=user_id or uuid.uuid4(), title="Test Hearing")
    t.__dict__.setdefault("transcription_jobs", [])
    t.__dict__.setdefault("minute_versions", [])
    t.__dict__.setdefault("tags", [])
    return t


def test_transcript_viewed_logs_audit_event(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription_id = uuid.uuid4()
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=_make_transcription(user.id))
    mock_logger = mocker.patch("transcribe_api.api.routes_dictation.logger")
    response = client.get(f"/api/transcriptions/{transcription_id}")
    assert response.status_code == 200
    log_messages = [call[0][0] for call in mock_logger.info.call_args_list]
    assert any("TRANSCRIPT_VIEWED" in msg for msg in log_messages)


def test_transcript_viewed_log_contains_transcription_id_and_user(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription_id = uuid.uuid4()
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=_make_transcription(user.id))
    mock_logger = mocker.patch("transcribe_api.api.routes_dictation.logger")
    client.get(f"/api/transcriptions/{transcription_id}")
    viewed_call = next(c for c in mock_logger.info.call_args_list if "TRANSCRIPT_VIEWED" in c[0][0])
    args = viewed_call[0]
    assert str(transcription_id) in str(args[1])
    assert user.email in str(args[3])


def test_transcript_deleted_logs_audit_event(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription_id = uuid.uuid4()
    mocker.patch("transcribe_api.api.routes_dictation.delete_transcription_by_id")
    mock_logger = mocker.patch("transcribe_api.api.routes_dictation.logger")
    response = client.delete(f"/api/transcriptions/{transcription_id}")
    assert response.status_code == 204
    log_messages = [call[0][0] for call in mock_logger.info.call_args_list]
    assert any("TRANSCRIPT_DELETED" in msg for msg in log_messages)


def test_transcript_deleted_log_contains_transcription_id_and_user(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription_id = uuid.uuid4()
    mocker.patch("transcribe_api.api.routes_dictation.delete_transcription_by_id")
    mock_logger = mocker.patch("transcribe_api.api.routes_dictation.logger")
    client.delete(f"/api/transcriptions/{transcription_id}")
    deleted_call = next(c for c in mock_logger.info.call_args_list if "TRANSCRIPT_DELETED" in c[0][0])
    args = deleted_call[0]
    assert str(transcription_id) in str(args[1])
    assert user.email in str(args[3])


def test_transcript_job_submitted_logs_audit_event(client, as_user, mocker):
    from transcribe_api.domain.models_dictation import TranscriptionJob
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription_id = uuid.uuid4()
    job_id = uuid.uuid4()
    mock_job = TranscriptionJob(id=job_id, transcription_id=transcription_id, dialogue_entries=[])
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=MagicMock())
    mocker.patch("transcribe_api.api.routes_dictation.save_transcription_job", return_value=mock_job)
    mock_logger = mocker.patch("transcribe_api.api.routes_dictation.logger")
    response = client.post(
        f"/api/transcriptions/{transcription_id}/jobs",
        json={"transcription_id": str(transcription_id), "dialogue_entries": []},
    )
    assert response.status_code == 200
    log_messages = [call[0][0] for call in mock_logger.info.call_args_list]
    assert any("TRANSCRIPT_JOB_SUBMITTED" in msg for msg in log_messages)


def test_transcript_job_submitted_log_contains_job_id_and_user(client, as_user, mocker):
    from transcribe_api.domain.models_dictation import TranscriptionJob
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription_id = uuid.uuid4()
    job_id = uuid.uuid4()
    mock_job = TranscriptionJob(id=job_id, transcription_id=transcription_id, dialogue_entries=[])
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=MagicMock())
    mocker.patch("transcribe_api.api.routes_dictation.save_transcription_job", return_value=mock_job)
    mock_logger = mocker.patch("transcribe_api.api.routes_dictation.logger")
    client.post(
        f"/api/transcriptions/{transcription_id}/jobs",
        json={"transcription_id": str(transcription_id), "dialogue_entries": []},
    )
    submitted_call = next(c for c in mock_logger.info.call_args_list if "TRANSCRIPT_JOB_SUBMITTED" in c[0][0])
    args = submitted_call[0]
    assert str(job_id) in str(args[2])
    assert user.email in str(args[4])


# ---------------------------------------------------------------------------
# TTL enforcement — ST-J6
# ---------------------------------------------------------------------------

def _mock_settings(mocker, download_ttl=30, upload_ttl=120):
    mock = mocker.MagicMock()
    mock.DOWNLOAD_URL_TTL_MINUTES = download_ttl
    mock.UPLOAD_URL_TTL_MINUTES = upload_ttl
    mock.AZURE_STORAGE_CONTAINER_NAME = "container"
    mock.DIRECT_SDK_ACCESS = True
    mock.APP_URL = "https://app.example.com"
    return mock


def test_generate_upload_url_uses_configured_ttl(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    settings = _mock_settings(mocker, upload_ttl=120)
    mocker.patch("transcribe_api.api.routes_dictation.get_settings", return_value=settings)
    mock_gen = mocker.patch("transcribe_api.api.routes_dictation.generate_blob_upload_url", new=AsyncMock(return_value="https://upload"))
    client.post("/api/get-upload-url", json={"file_extension": "wav"})
    _, kwargs = mock_gen.call_args
    assert kwargs["expiry_hours"] == pytest.approx(120 / 60)


def test_get_document_download_url_uses_configured_ttl(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription_id = uuid.uuid4()
    transcription = _make_transcription(user.id)
    job = MagicMock()
    job.s3_audio_url = f"user-uploads/{user.azure_user_id}/file.docx"
    transcription.__dict__["transcription_jobs"] = [job]
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    settings = _mock_settings(mocker, download_ttl=30)
    mocker.patch("transcribe_api.api.routes_dictation.get_settings", return_value=settings)
    mock_gen = mocker.patch("transcribe_api.api.routes_dictation.generate_blob_download_url", new=AsyncMock(return_value="https://dl"))
    client.get(f"/api/transcriptions/{transcription_id}/document-download")
    _, kwargs = mock_gen.call_args
    assert kwargs["expiry_hours"] == pytest.approx(30 / 60)
