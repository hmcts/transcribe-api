"""Route-level tests for api/privileged_routes.py.

Auth is mocked via dependency_overrides. File I/O (_read_content / _write_content)
is mocked so tests don't touch the filesystem.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from transcribe_api.domain.models_dictation import Transcription, TranscriptionJob, User


def _make_user(roles: list[str] | None = None, email: str = "admin@justice.gov.uk") -> User:
    user = User(id=uuid.uuid4(), email=email, azure_user_id="admin-oid")
    user.__dict__["app_roles"] = roles or []
    return user


def _make_transcription(owner: User | None = None) -> Transcription:
    owner = owner or _make_user(roles=["Judge"], email="judge@justice.gov.uk")
    t = Transcription(
        id=uuid.uuid4(),
        user_id=owner.id,
        title="Test Hearing",
        created_datetime=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
    )
    t.user = owner
    t.transcription_jobs = []
    t.tags = []
    return t


def _make_transcription_with_doc(owner: User | None = None) -> Transcription:
    owner = owner or _make_user(roles=["Judge"], email="judge@justice.gov.uk")
    t = _make_transcription(owner)
    job = TranscriptionJob(
        id=uuid.uuid4(),
        transcription_id=t.id,
        dialogue_entries=[],
        s3_audio_url="user-uploads/judge@justice.gov.uk/hearing-document-20260115.docx",
    )
    t.transcription_jobs = [job]
    return t


SAMPLE_CONTENT = {
    "hearing_types": [
        {
            "id": "face_to_face",
            "title": "Face to face",
            "hearing_title": "Face to Face Hearing",
            "hearing_description": "A hearing in person.",
        }
    ],
    "legal_frameworks": [
        {
            "id": "eea_deportation",
            "title": "EEA Deportation",
            "rank": 1,
            "content": "Framework content.",
            "issues": [],
        }
    ],
    "anonymity": {
        "statuses": [
            {
                "id": "granted",
                "title": "Granted",
                "content": "Content.",
                "identifier": "",
                "header": "Header",
                "order_header": "Order header",
                "order_text": "Order text.",
            }
        ]
    },
}


@pytest.fixture
def app():
    from fastapi import FastAPI

    from transcribe_api.api.routes_dictation_privileged import router as admin_router
    test_app = FastAPI()
    test_app.include_router(admin_router, prefix="/api")
    return test_app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def as_user(app):
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user
    yield lambda user: app.dependency_overrides.__setitem__(get_current_user, lambda: user)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def mock_content(mocker):
    """Mock file I/O so tests don't touch the filesystem."""
    import copy
    data = copy.deepcopy(SAMPLE_CONTENT)
    mocker.patch("transcribe_api.api.routes_dictation_privileged._read_content", return_value=data)
    mocker.patch("transcribe_api.api.routes_dictation_privileged._write_content")
    return data


# ---------------------------------------------------------------------------
# GET /api/admin/me — role guard
# ---------------------------------------------------------------------------

def test_admin_me_returns_200_for_legal_text_manager(client, as_user):
    as_user(_make_user(roles=["LegalTextManager"]))
    response = client.get("/api/admin/me")
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "admin@justice.gov.uk"
    assert body["is_admin"] is True


def test_admin_me_returns_200_for_system_administrator(client, as_user):
    as_user(_make_user(roles=["SystemAdministrator"]))
    response = client.get("/api/admin/me")
    assert response.status_code == 200


def test_admin_me_returns_403_for_judge_only(client, as_user):
    as_user(_make_user(roles=["Judge"]))
    response = client.get("/api/admin/me")
    assert response.status_code == 403


def test_admin_me_returns_403_for_unauthenticated_user(client, as_user):
    as_user(_make_user(roles=[]))
    response = client.get("/api/admin/me")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/admin/document-content
# ---------------------------------------------------------------------------

def test_get_document_content_returns_full_payload(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    response = client.get("/api/admin/document-content")
    assert response.status_code == 200
    body = response.json()
    assert body["hearing_types"][0]["id"] == "face_to_face"
    assert body["legal_frameworks"][0]["id"] == "eea_deportation"


def test_get_document_content_returns_403_for_judge(client, as_user, mock_content):
    as_user(_make_user(roles=["Judge"]))
    response = client.get("/api/admin/document-content")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# PUT /api/admin/document-content/hearing-types/{item_id}
# ---------------------------------------------------------------------------

def test_update_hearing_type_returns_updated_item(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    payload = {
        "id": "face_to_face",
        "title": "Face to Face Updated",
        "hearing_title": "Updated Title",
        "hearing_description": "Updated description.",
    }
    response = client.put("/api/admin/document-content/hearing-types/face_to_face", json=payload)
    assert response.status_code == 200
    assert response.json()["title"] == "Face to Face Updated"


def test_update_hearing_type_returns_404_when_not_found(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    payload = {
        "id": "nonexistent",
        "title": "X",
        "hearing_title": "X",
        "hearing_description": "X",
    }
    response = client.put("/api/admin/document-content/hearing-types/nonexistent", json=payload)
    assert response.status_code == 404


def test_update_hearing_type_returns_403_without_role(client, as_user, mock_content):
    as_user(_make_user(roles=["Judge"]))
    payload = {
        "id": "face_to_face",
        "title": "X",
        "hearing_title": "X",
        "hearing_description": "X",
    }
    response = client.put("/api/admin/document-content/hearing-types/face_to_face", json=payload)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/admin/document-content/hearing-types
# ---------------------------------------------------------------------------

def test_create_hearing_type_returns_201(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    payload = {
        "id": "new_type",
        "title": "New Type",
        "hearing_title": "New Hearing Title",
        "hearing_description": "New description.",
    }
    response = client.post("/api/admin/document-content/hearing-types", json=payload)
    assert response.status_code == 201
    assert response.json()["id"] == "new_type"


def test_create_hearing_type_returns_409_for_duplicate(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    payload = {
        "id": "face_to_face",  # already exists in SAMPLE_CONTENT
        "title": "Duplicate",
        "hearing_title": "Duplicate",
        "hearing_description": "Duplicate.",
    }
    response = client.post("/api/admin/document-content/hearing-types", json=payload)
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# DELETE /api/admin/document-content/hearing-types/{item_id}
# ---------------------------------------------------------------------------

def test_delete_hearing_type_returns_204(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    response = client.delete("/api/admin/document-content/hearing-types/face_to_face")
    assert response.status_code == 204


def test_delete_hearing_type_returns_404_when_not_found(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    response = client.delete("/api/admin/document-content/hearing-types/nonexistent")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/admin/document-content/legal-frameworks/{item_id}
# ---------------------------------------------------------------------------

def test_update_legal_framework_returns_updated_item(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    payload = {
        "id": "eea_deportation",
        "title": "Updated Framework",
        "rank": 1,
        "content": "Updated content.",
        "issues": [],
    }
    response = client.put("/api/admin/document-content/legal-frameworks/eea_deportation", json=payload)
    assert response.status_code == 200
    assert response.json()["title"] == "Updated Framework"


def test_update_legal_framework_returns_404_when_not_found(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    payload = {
        "id": "nonexistent",
        "title": "X",
        "rank": 1,
        "content": "X",
        "issues": [],
    }
    response = client.put("/api/admin/document-content/legal-frameworks/nonexistent", json=payload)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/admin/document-content/legal-frameworks
# ---------------------------------------------------------------------------

def test_create_legal_framework_returns_201(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    payload = {
        "id": "new_framework",
        "title": "New Framework",
        "rank": 2,
        "content": "Content.",
        "issues": [],
    }
    response = client.post("/api/admin/document-content/legal-frameworks", json=payload)
    assert response.status_code == 201
    assert response.json()["id"] == "new_framework"


def test_create_legal_framework_returns_409_for_duplicate(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    payload = {
        "id": "eea_deportation",  # already exists
        "title": "Duplicate",
        "rank": 1,
        "content": "Duplicate.",
        "issues": [],
    }
    response = client.post("/api/admin/document-content/legal-frameworks", json=payload)
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# DELETE /api/admin/document-content/legal-frameworks/{item_id}
# ---------------------------------------------------------------------------

def test_delete_legal_framework_returns_204(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    response = client.delete("/api/admin/document-content/legal-frameworks/eea_deportation")
    assert response.status_code == 204


def test_delete_legal_framework_returns_404_when_not_found(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    response = client.delete("/api/admin/document-content/legal-frameworks/nonexistent")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/admin/document-content/anonymity-statuses/{item_id}
# ---------------------------------------------------------------------------

def test_update_anonymity_status_returns_200(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    payload = {
        "id": "granted",
        "title": "Updated Granted",
        "content": "Updated content.",
        "identifier": "",
        "header": "Header",
        "order_header": "Order header",
        "order_text": "Order text.",
    }
    response = client.put("/api/admin/document-content/anonymity-statuses/granted", json=payload)
    assert response.status_code == 200
    assert response.json()["title"] == "Updated Granted"


def test_update_anonymity_status_returns_404_when_not_found(client, as_user, mock_content):
    as_user(_make_user(roles=["LegalTextManager"]))
    payload = {
        "id": "nonexistent",
        "title": "X",
        "content": "X",
        "identifier": "",
        "header": "X",
        "order_header": "X",
        "order_text": "X",
    }
    response = client.put("/api/admin/document-content/anonymity-statuses/nonexistent", json=payload)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/admin/transcriptions
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_admin_list(mocker):
    owner = _make_user(roles=["Judge"], email="judge@justice.gov.uk")
    transcription = _make_transcription(owner)
    mocker.patch(
        "transcribe_api.api.routes_dictation_privileged.admin_fetch_all_transcriptions",
        return_value=([transcription], 1),
    )
    return transcription


def test_admin_list_transcriptions_returns_200_for_system_administrator(client, as_user, mock_admin_list):
    as_user(_make_user(roles=["SystemAdministrator"]))
    response = client.get("/api/admin/transcriptions")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["owner_email"] == "judge@justice.gov.uk"
    assert item["title"] == "Test Hearing"


def test_admin_list_transcriptions_returns_403_for_judge(client, as_user, mock_admin_list):
    as_user(_make_user(roles=["Judge"]))
    response = client.get("/api/admin/transcriptions")
    assert response.status_code == 403


def test_admin_list_transcriptions_returns_403_for_legal_text_manager(client, as_user, mock_admin_list):
    as_user(_make_user(roles=["LegalTextManager"]))
    response = client.get("/api/admin/transcriptions")
    assert response.status_code == 403


def test_admin_list_transcriptions_writes_audit_log(client, as_user, mock_admin_list, mocker):
    as_user(_make_user(roles=["SystemAdministrator"]))
    mock_log = mocker.patch("transcribe_api.api.routes_dictation_privileged._log_admin_transcript_access")
    client.get("/api/admin/transcriptions")
    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["action"] == "LIST_TRANSCRIPTIONS"


# ---------------------------------------------------------------------------
# GET /api/admin/transcriptions/{id}
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_admin_get(mocker):
    owner = _make_user(roles=["Judge"], email="judge@justice.gov.uk")
    transcription = _make_transcription(owner)
    mocker.patch(
        "transcribe_api.api.routes_dictation_privileged.admin_get_transcription_by_id",
        return_value=transcription,
    )
    mocker.patch("transcribe_api.api.routes_dictation_privileged.get_user_by_id", return_value=owner)
    return transcription, owner


def test_admin_get_transcription_returns_200_for_system_administrator(client, as_user, mock_admin_get):
    as_user(_make_user(roles=["SystemAdministrator"]))
    transcription, _ = mock_admin_get
    response = client.get(f"/api/admin/transcriptions/{transcription.id}")
    assert response.status_code == 200
    assert response.json()["title"] == "Test Hearing"


def test_admin_get_transcription_returns_403_for_judge(client, as_user, mock_admin_get):
    as_user(_make_user(roles=["Judge"]))
    transcription, _ = mock_admin_get
    response = client.get(f"/api/admin/transcriptions/{transcription.id}")
    assert response.status_code == 403


def test_admin_get_transcription_returns_404_when_not_found(client, as_user, mocker):
    as_user(_make_user(roles=["SystemAdministrator"]))
    from fastapi import HTTPException
    mocker.patch(
        "transcribe_api.api.routes_dictation_privileged.admin_get_transcription_by_id",
        side_effect=HTTPException(status_code=404, detail="Transcription not found"),
    )
    response = client.get(f"/api/admin/transcriptions/{uuid.uuid4()}")
    assert response.status_code == 404


def test_admin_get_transcription_writes_audit_log(client, as_user, mock_admin_get, mocker):
    as_user(_make_user(roles=["SystemAdministrator"]))
    transcription, _ = mock_admin_get
    mock_log = mocker.patch("transcribe_api.api.routes_dictation_privileged._log_admin_transcript_access")
    client.get(f"/api/admin/transcriptions/{transcription.id}")
    mock_log.assert_called_once()
    call_kwargs = mock_log.call_args.kwargs
    assert call_kwargs["action"] == "VIEW_TRANSCRIPTION"
    assert call_kwargs["target_transcription_id"] == str(transcription.id)


# ---------------------------------------------------------------------------
# GET /api/admin/transcriptions/{id}/document-download
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_admin_download(mocker):
    owner = _make_user(roles=["Judge"], email="judge@justice.gov.uk")
    transcription = _make_transcription_with_doc(owner)
    mocker.patch(
        "transcribe_api.api.routes_dictation_privileged.admin_get_transcription_by_id",
        return_value=transcription,
    )
    mocker.patch("transcribe_api.api.routes_dictation_privileged.get_user_by_id", return_value=owner)
    mocker.patch(
        "transcribe_api.api.routes_dictation_privileged.generate_blob_download_url",
        new=AsyncMock(return_value="https://storage.example.com/doc.docx?sas=token"),
    )
    settings_mock = mocker.patch("transcribe_api.api.routes_dictation_privileged.get_settings")
    settings_mock.return_value.AZURE_STORAGE_CONTAINER_NAME = "application-data"
    return transcription, owner


def test_admin_document_download_returns_200_for_system_administrator(client, as_user, mock_admin_download):
    as_user(_make_user(roles=["SystemAdministrator"]))
    transcription, _ = mock_admin_download
    response = client.get(f"/api/admin/transcriptions/{transcription.id}/document-download")
    assert response.status_code == 200
    assert "download_url" in response.json()
    assert response.json()["download_url"] == "https://storage.example.com/doc.docx?sas=token"


def test_admin_document_download_returns_403_for_judge(client, as_user, mock_admin_download):
    as_user(_make_user(roles=["Judge"]))
    transcription, _ = mock_admin_download
    response = client.get(f"/api/admin/transcriptions/{transcription.id}/document-download")
    assert response.status_code == 403


def test_admin_document_download_returns_404_when_no_document(client, as_user, mocker):
    as_user(_make_user(roles=["SystemAdministrator"]))
    owner = _make_user(roles=["Judge"], email="judge@justice.gov.uk")
    transcription = _make_transcription(owner)  # no transcription_jobs → no blob_path
    mocker.patch("transcribe_api.api.routes_dictation_privileged.admin_get_transcription_by_id", return_value=transcription)
    mocker.patch("transcribe_api.api.routes_dictation_privileged.get_user_by_id", return_value=owner)
    response = client.get(f"/api/admin/transcriptions/{transcription.id}/document-download")
    assert response.status_code == 404


def test_admin_document_download_writes_audit_log(client, as_user, mock_admin_download, mocker):
    as_user(_make_user(roles=["SystemAdministrator"]))
    transcription, _ = mock_admin_download
    mock_log = mocker.patch("transcribe_api.api.routes_dictation_privileged._log_admin_transcript_access")
    client.get(f"/api/admin/transcriptions/{transcription.id}/document-download")
    mock_log.assert_called_once()
    call_kwargs = mock_log.call_args.kwargs
    assert call_kwargs["action"] == "DOWNLOAD_DOCUMENT"


# ---------------------------------------------------------------------------
# TTL enforcement — ST-J6
# ---------------------------------------------------------------------------

def test_admin_document_download_uses_configured_ttl(client, as_user, mock_admin_download, mocker):
    as_user(_make_user(roles=["SystemAdministrator"]))
    transcription, _ = mock_admin_download
    mocker.patch("transcribe_api.api.routes_dictation_privileged._log_admin_transcript_access")
    mock_generate = mocker.patch(
        "transcribe_api.api.routes_dictation_privileged.generate_blob_download_url",
        new_callable=AsyncMock,
        return_value="https://blob/url",
    )
    mocker.patch("transcribe_api.api.routes_dictation_privileged.get_settings", return_value=mocker.MagicMock(
        AZURE_STORAGE_CONTAINER_NAME="container",
        DOWNLOAD_URL_TTL_MINUTES=30,
    ))
    client.get(f"/api/admin/transcriptions/{transcription.id}/document-download")
    _, kwargs = mock_generate.call_args
    assert kwargs["expiry_hours"] == pytest.approx(30 / 60)
