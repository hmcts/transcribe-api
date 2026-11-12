"""Route-level tests for api/privileged_routes.py.

Auth is mocked via dependency_overrides. File I/O (_read_content / _write_content)
is mocked so tests don't touch the filesystem.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tests.unit.jurisdiction_fixtures import IAC_JURISDICTION_ID
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
        jurisdiction_id=IAC_JURISDICTION_ID,  # the admin works in the IAC too (DIAAT-334)
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
# GET /api/admin/me — admin.access
# ---------------------------------------------------------------------------


def test_admin_me_returns_200_for_legal_text_manager(client, as_user):
    as_user(_make_user(roles=["LegalTextManager"]))
    response = client.get("/api/admin/me")
    assert response.status_code == 200
    assert response.json() == {
        "email": "admin@justice.gov.uk",
        "role": "LegalTextManager",
        "user_id": response.json()["user_id"],
    }


@pytest.mark.parametrize("role", ["JurisdictionAdministrator", "JurisdictionSuperAdministrator"])
def test_admin_me_returns_200_for_the_administrator_roles(client, as_user, role):
    as_user(_make_user(roles=[role]))
    assert client.get("/api/admin/me").status_code == 200


def test_admin_me_returns_200_for_a_system_administrator_acting_as_one(client, as_user):
    from transcribe_api.domain.auth.app_token import Context
    from transcribe_api.domain.auth.auth_models_dictation import AuthenticatedUser
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        db_user=_make_user(), ctx=Context(None, "SystemAdministrator"), sysadmin=True
    )
    response = client.get("/api/admin/me")
    assert response.status_code == 200
    assert response.json()["role"] == "SystemAdministrator"


def test_admin_me_returns_403_for_a_system_administrator_acting_as_a_judge(client, as_user):
    from transcribe_api.domain.auth.app_token import Context
    from transcribe_api.domain.auth.auth_models_dictation import AuthenticatedUser
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    client.app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        db_user=_make_user(), ctx=Context("IAC", "Judge"), sysadmin=True
    )
    response = client.get("/api/admin/me")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_denied"


def test_admin_me_returns_403_for_judge_only(client, as_user):
    as_user(_make_user(roles=["Judge"]))
    response = client.get("/api/admin/me")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# GET /api/admin/roles — {"roles": [...]} (DIAAT-374)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "sysadmin", "expected"),
    [
        ("JurisdictionAdministrator", False, ["Judge", "LegalTextManager"]),
        ("JurisdictionSuperAdministrator", False, ["JurisdictionAdministrator"]),
        ("SystemAdministrator", True, ["JurisdictionSuperAdministrator"]),
        ("JurisdictionSuperAdministrator", True, ["JurisdictionAdministrator"]),
    ],
)
def test_admin_roles_is_an_object_with_the_roles_the_caller_may_assign(client, app, role, sysadmin, expected):
    from transcribe_api.domain.auth.app_token import Context
    from transcribe_api.domain.auth.auth_models_dictation import AuthenticatedUser
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    ctx = Context(None if role in ("JurisdictionSuperAdministrator", "SystemAdministrator") else "IAC", role)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        db_user=_make_user(), ctx=ctx, sysadmin=sysadmin
    )

    response = client.get("/api/admin/roles")

    assert response.status_code == 200
    # The shape the web app reads (DIAAT-374): an object, not a bare array.
    assert list(response.json()) == ["roles"]
    assert response.json()["roles"] == expected
    assert "SystemAdministrator" not in response.json()["roles"]
    assert "Normal" not in response.json()["roles"]


@pytest.mark.parametrize("role", ["Judge", "LegalTextManager"])
def test_admin_roles_is_refused_to_those_who_assign_nothing(client, as_user, role):
    as_user(_make_user(roles=[role]))
    assert client.get("/api/admin/roles").status_code == 403


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


@pytest.mark.parametrize(
    "role", ["Judge", "JurisdictionAdministrator", "JurisdictionSuperAdministrator", "SystemAdministrator"]
)
def test_get_document_content_returns_403_for_everyone_but_the_legal_text_manager(client, as_user, mock_content, role):
    # legal_text.manage is the LegalTextManager's alone (DIAAT-376, DIAAT-389): not an administrator's, not the Entra role's.
    as_user(_make_user(roles=[role]))
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


@pytest.mark.parametrize("role", ["Judge", "JurisdictionAdministrator", "JurisdictionSuperAdministrator"])
def test_update_hearing_type_returns_403_for_everyone_but_the_legal_text_manager(client, as_user, mock_content, role):
    as_user(_make_user(roles=[role]))
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
# DIAAT-389: /api/admin/transcriptions* are gone: no role needs another user's dictation records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("roles", [["SystemAdministrator"], ["LegalTextManager"], ["Judge"]])
def test_the_admin_transcription_endpoints_no_longer_exist(client, as_user, roles):
    as_user(_make_user(roles=roles))
    some_id = uuid.uuid4()
    for path in (
        "/api/admin/transcriptions",
        f"/api/admin/transcriptions/{some_id}",
        f"/api/admin/transcriptions/{some_id}/document-download",
    ):
        assert client.get(path).status_code == 404, path


def test_the_admin_user_role_and_delete_endpoints_no_longer_exist(client, as_user):
    as_user(_make_user(roles=["SystemAdministrator"]))
    user_id = uuid.uuid4()
    assert client.put(f"/api/admin/users/{user_id}/role", json={"role": "Judge"}).status_code == 404
    # Nor is there a path by a user's id any more (users are listed with their assignments: DIAAT-383).
    assert client.delete(f"/api/admin/users/{user_id}").status_code == 404
    assert client.get(f"/api/admin/users/{user_id}").status_code == 404
