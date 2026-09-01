"""Tests for the live-draft autosave endpoints (GET/PUT/DELETE /api/live-draft).

Auth is mocked via dependency_overrides. DB calls are mocked via mocker.patch so no
real database connection is needed.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from transcribe_api.domain.models_dictation import LiveTranscriptDraft, User


def _make_user(roles: list[str] | None = None) -> User:
    user = User(id=uuid.uuid4(), email="test@justice.gov.uk", azure_user_id="test-oid")
    user.__dict__["app_roles"] = roles or []
    user.has_completed_onboarding = True
    return user


def _make_draft(user_id, transcript: dict | None = None, form_data: dict | None = None, age_seconds: int = 60) -> LiveTranscriptDraft:
    saved_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    draft = LiveTranscriptDraft(
        user_id=user_id,
        transcript=transcript or {"background": [], "evidence": [], "facts": []},
        form_data=form_data or {},
    )
    draft.updated_datetime = saved_at
    draft.created_datetime = saved_at
    return draft


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
def client_no_raise(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_user(app):
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user
    yield lambda user: app.dependency_overrides.__setitem__(get_current_user, lambda: user)
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# GET /api/live-draft
# ---------------------------------------------------------------------------

def test_get_live_draft_returns_404_when_no_draft(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.get_live_draft", return_value=None)
    response = client.get("/api/live-draft")
    assert response.status_code == 404


def test_get_live_draft_returns_draft_when_found(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    transcript = {"background": [{"speaker": "Judge", "text": "Proceedings commenced", "timestamp": "00:00"}], "evidence": [], "facts": []}
    form_data = {"hearingType": "appeal"}
    draft = _make_draft(user.id, transcript=transcript, form_data=form_data, age_seconds=300)
    mocker.patch("transcribe_api.api.routes_dictation.get_live_draft", return_value=draft)

    response = client.get("/api/live-draft")

    assert response.status_code == 200
    body = response.json()
    assert body["transcript"] == transcript
    assert body["form_data"] == form_data
    assert "saved_at" in body


def test_get_live_draft_passes_current_user_id(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mock_get = mocker.patch("transcribe_api.api.routes_dictation.get_live_draft", return_value=None)

    client.get("/api/live-draft")

    mock_get.assert_called_once_with(user.id)


def test_get_live_draft_returns_401_when_unauthenticated(client):
    response = client.get("/api/live-draft")
    assert response.status_code in (401, 403, 422)


def test_get_live_draft_saved_at_is_iso_datetime(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    draft = _make_draft(user.id, age_seconds=120)
    mocker.patch("transcribe_api.api.routes_dictation.get_live_draft", return_value=draft)

    body = client.get("/api/live-draft").json()

    # saved_at should be a parseable ISO datetime string
    saved_at = datetime.fromisoformat(body["saved_at"])
    assert saved_at.tzinfo is not None


# ---------------------------------------------------------------------------
# PUT /api/live-draft
# ---------------------------------------------------------------------------

def test_put_live_draft_returns_204(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.upsert_live_draft")
    payload = {
        "transcript": {"background": [], "evidence": [], "facts": []},
        "form_data": {"hearingType": "appeal"},
    }

    response = client.put("/api/live-draft", json=payload)

    assert response.status_code == 204


def test_put_live_draft_calls_upsert_with_correct_args(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mock_upsert = mocker.patch("transcribe_api.api.routes_dictation.upsert_live_draft")
    transcript = {"background": [{"speaker": "Judge", "text": "Test", "timestamp": "00:00"}], "evidence": [], "facts": []}
    form_data = {"hearingType": "appeal", "caseName": "Smith v Jones"}
    payload = {"transcript": transcript, "form_data": form_data}

    client.put("/api/live-draft", json=payload)

    mock_upsert.assert_called_once_with(user.id, transcript, form_data)


def test_put_live_draft_returns_401_when_unauthenticated(client):
    payload = {"transcript": {}, "form_data": {}}
    response = client.put("/api/live-draft", json=payload)
    assert response.status_code in (401, 403, 422)


def test_put_live_draft_returns_422_on_missing_body(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.upsert_live_draft")

    response = client.put("/api/live-draft")

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/live-draft
# ---------------------------------------------------------------------------

def test_delete_live_draft_returns_204(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.delete_live_draft")

    response = client.delete("/api/live-draft")

    assert response.status_code == 204


def test_delete_live_draft_calls_delete_with_user_id(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mock_delete = mocker.patch("transcribe_api.api.routes_dictation.delete_live_draft")

    client.delete("/api/live-draft")

    mock_delete.assert_called_once_with(user.id)


def test_delete_live_draft_returns_204_even_when_no_draft_exists(client, as_user, mocker):
    """DELETE is idempotent — 204 whether or not a draft existed."""
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.delete_live_draft")  # no-op (delete_live_draft handles missing draft)

    response = client.delete("/api/live-draft")

    assert response.status_code == 204


def test_delete_live_draft_returns_401_when_unauthenticated(client):
    response = client.delete("/api/live-draft")
    assert response.status_code in (401, 403, 422)


# ---------------------------------------------------------------------------
# DB exception propagation — unhandled errors must surface as 500
# ---------------------------------------------------------------------------

def test_get_live_draft_returns_500_when_db_raises(client_no_raise, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.get_live_draft", side_effect=Exception("DB unavailable"))
    response = client_no_raise.get("/api/live-draft")
    assert response.status_code == 500


def test_put_live_draft_returns_500_when_db_raises(client_no_raise, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.upsert_live_draft", side_effect=Exception("DB unavailable"))
    payload = {"transcript": {"background": [], "evidence": [], "facts": []}, "form_data": {}}
    response = client_no_raise.put("/api/live-draft", json=payload)
    assert response.status_code == 500


def test_delete_live_draft_returns_500_when_db_raises(client_no_raise, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    mocker.patch("transcribe_api.api.routes_dictation.delete_live_draft", side_effect=Exception("DB unavailable"))
    response = client_no_raise.delete("/api/live-draft")
    assert response.status_code == 500
