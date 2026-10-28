"""SEC-1: client-supplied IDs can never move another user's records to the caller."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transcribe_api.domain.exceptions import OwnershipConflictError
from transcribe_api.domain.models_dictation import User

ROUTES = "transcribe_api.api.routes_dictation"
TEST_OID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
VICTIM_TRANSCRIPTION = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"


def _user() -> User:
    user = User(id=uuid.uuid4(), email="judge@justice.gov.uk", azure_user_id=TEST_OID)
    user.__dict__["app_roles"] = ["Judge"]
    return user


@pytest.fixture
def app():
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


def _blob_manager(mocker):
    manager = mocker.AsyncMock()
    manager.create_blob_from_bytes = mocker.AsyncMock(return_value=True)
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch(f"{ROUTES}.AsyncAzureBlobManager", return_value=manager)
    return manager


def _put(client, name):
    return client.put("/api/upload-audio", params={"key": f"user-uploads/{TEST_OID}/{name}"}, content=b"audio")


# Proxy upload: the file name's UUID becomes the transcription ID ------------------------


def test_proxy_upload_naming_another_users_transcription_is_refused(client, as_user, mocker):
    as_user(_user())
    manager = _blob_manager(mocker)
    mocker.patch(f"{ROUTES}.get_transcription_owner", return_value=uuid.uuid4())
    assert _put(client, f"{VICTIM_TRANSCRIPTION}.mp3").status_code == 403
    manager.create_blob_from_bytes.assert_not_awaited()


@pytest.mark.parametrize("owner", ["self", None])
def test_proxy_upload_of_own_or_new_id_is_stored(client, as_user, mocker, owner):
    user = _user()
    as_user(user)
    manager = _blob_manager(mocker)
    mocker.patch(f"{ROUTES}.get_transcription_owner", return_value=user.id if owner == "self" else None)
    assert _put(client, f"{VICTIM_TRANSCRIPTION}.mp3").status_code == 200
    manager.create_blob_from_bytes.assert_awaited_once()


def test_proxy_upload_lookup_failure_or_non_uuid_name_does_not_block(client, as_user, mocker):
    as_user(_user())
    manager = _blob_manager(mocker)
    lookup = mocker.patch(f"{ROUTES}.get_transcription_owner", side_effect=RuntimeError("db down"))
    assert _put(client, f"{VICTIM_TRANSCRIPTION}.mp3").status_code == 200  # the save check still applies
    assert _put(client, "hearing.mp3").status_code == 200
    assert lookup.call_count == 1  # no lookup for a non-UUID name
    assert manager.create_blob_from_bytes.await_count == 2


# Saves with a client-supplied ID --------------------------------------------------------


def test_saving_another_users_transcription_is_not_found(client, as_user, mocker):
    as_user(_user())
    mocker.patch(f"{ROUTES}.save_transcription", side_effect=OwnershipConflictError("Transcription"))
    response = client.post("/api/transcriptions", json={"id": VICTIM_TRANSCRIPTION, "title": "mine now"})
    assert response.status_code == 404


def test_job_is_saved_under_the_transcription_in_the_path(client, as_user, mocker):
    as_user(_user())
    mine = uuid.uuid4()
    mocker.patch(f"{ROUTES}.get_transcription_by_id", return_value=MagicMock())
    save = mocker.patch(f"{ROUTES}.save_transcription_job", side_effect=lambda job, **_: job)
    body = {"transcription_id": VICTIM_TRANSCRIPTION, "dialogue_entries": []}
    client.post(f"/api/transcriptions/{mine}/jobs", json=body)
    assert save.call_args.kwargs["job"].transcription_id == mine


def test_job_or_minute_version_owned_elsewhere_is_not_found(client, as_user, mocker):
    as_user(_user())
    mine = uuid.uuid4()
    mocker.patch(f"{ROUTES}.get_transcription_by_id", return_value=MagicMock())
    mocker.patch(f"{ROUTES}.save_transcription_job", side_effect=OwnershipConflictError("TranscriptionJob"))
    mocker.patch(f"{ROUTES}.save_minute_version", side_effect=OwnershipConflictError("MinuteVersion"))
    job = client.post(f"/api/transcriptions/{mine}/jobs", json={"transcription_id": str(mine), "dialogue_entries": []})
    assert job.status_code == 404
    minutes = client.post(
        f"/api/transcriptions/{mine}/minute-versions",
        json={
            "transcription_id": str(mine),
            "html_content": "x",
            "template": {"name": "General", "description": "d", "category": "c"},
        },
    )
    assert minutes.status_code == 404


# The upload poller ---------------------------------------------------------------------


@pytest.fixture
def service():
    from transcribe_api.stt.work_poller import TranscriptionPollingService

    with (
        patch("transcribe_api.stt.work_poller.get_settings"),
        patch("transcribe_api.stt.work_poller.AsyncAzureBlobManager"),
    ):
        return TranscriptionPollingService()


@pytest.mark.asyncio
async def test_poller_never_retries_an_upload_naming_another_users_transcription(service, mocker):
    blob = f"user-uploads/{TEST_OID}/{VICTIM_TRANSCRIPTION}.mp3"
    service._mark_blob_in_progress = AsyncMock(return_value=True)
    service._mark_blob_with_error = AsyncMock()
    service._mark_blob_permanently_failed = AsyncMock()
    service.resolve_blob_owner_from_path = MagicMock(return_value=MagicMock(id=uuid.uuid4(), azure_user_id=TEST_OID))
    mocker.patch(
        "transcribe_api.stt.work_poller.transcribe_and_generate_llm_output",
        AsyncMock(side_effect=OwnershipConflictError("Transcription")),
    )

    assert await service.process_discovered_audio({"name": blob}) is False
    service._mark_blob_permanently_failed.assert_awaited_once()
    service._mark_blob_with_error.assert_not_awaited()
