"""DIAAT-276: journeys for live submissions, uploads and minutes in the dictation API."""

import asyncio
import uuid

import pytest
from hmcts_otel import conventions as c
from hmcts_otel import current_journey
from hmcts_otel.testing import capture_telemetry

from transcribe_api.runtime.telemetry import JOURNEY_SUBMISSION, JOURNEY_TRANSCRIPTION, transcription_id_from_key
from tests.unit.dictation.unit.api.test_live_transcription_submission_logging import (  # DIAAT-298 helpers
    _MINIMAL_BODY,
    _make_user,
    _patch_submission_deps,
)


def test_transcription_id_from_key_is_canonical():
    tid = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    assert transcription_id_from_key(f"user-uploads/oid/{tid}.mp4") == tid
    # Any form UUID() accepts is normalised, so upload, poller and database agree.
    assert transcription_id_from_key(f"user-uploads/oid/{tid.upper()}.mp4") == tid
    assert transcription_id_from_key(f"user-uploads/oid/{tid.replace('-', '')}.mp4") == tid
    assert transcription_id_from_key("user-uploads/oid/notes.mp4") is None
    assert transcription_id_from_key(None) is None


def test_live_submission_journey_relates_the_transcription(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    saved = _patch_submission_deps(mocker)
    with capture_telemetry() as telemetry:
        response = client.post("/api/live-transcription/submission", json=_MINIMAL_BODY)
        assert response.status_code == 200, response.text
        submission_events = [e for e in telemetry.events() if e.get(c.JOURNEY_TYPE) == JOURNEY_SUBMISSION]
        assert [e[c.CUSTOM_EVENT_NAME] for e in submission_events] == [
            "submission.started",
            "document_generation.started",
            "document_generation.completed",
            "transcript.available",
            "submission.completed",
        ]
        assert len({e[c.JOURNEY_ID] for e in submission_events}) == 1
        available = telemetry.events("transcript.available")[0]
        assert available["dictation.transcription_id"] == str(saved.id)


def test_upload_url_starts_the_transcription_journey(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    settings = mocker.MagicMock(DIRECT_SDK_ACCESS=False, APP_URL="http://localhost:8000")
    mocker.patch("transcribe_api.api.routes_dictation.get_settings", return_value=settings)
    with capture_telemetry() as telemetry:
        response = client.post("/api/get-upload-url", json={"file_extension": "mp4"})
        assert response.status_code == 200, response.text
        transcription_id = transcription_id_from_key(response.json()["user_upload_s3_file_key"])
        (issued,) = telemetry.events("upload.url_issued")
        assert issued[c.JOURNEY_TYPE] == JOURNEY_TRANSCRIPTION
        assert issued[c.JOURNEY_ID] == transcription_id
        assert issued["upload.method"] == "proxy"


@pytest.mark.parametrize("action", ["generate", "edit"])
def test_minutes_background_task_runs_in_the_transcription_journey(client, as_user, mocker, action):
    as_user(_make_user(roles=["Judge"]))
    transcription_id = str(uuid.uuid4())
    seen: dict = {}
    done = asyncio.Event()

    async def fake_task(*args, **kwargs):
        seen["journey"] = current_journey()
        done.set()

    mocker.patch(
        "transcribe_api.api.routes_dictation._validate_transcription_for_minutes", return_value=[{"text": "x"}]
    )
    mocker.patch("transcribe_api.api.routes_dictation.generate_llm_output_task", side_effect=fake_task)
    mocker.patch("transcribe_api.api.routes_dictation.ai_edit_task", side_effect=fake_task)
    body = {
        "transcription_id": transcription_id,
        "template": {"name": "General", "description": "d", "category": "common"},
        "action_type": action,
    }
    if action == "edit":
        body |= {"edit_instructions": "shorter", "current_minute_version_id": str(uuid.uuid4())}
    response = client.post("/api/generate-or-edit-minutes", json=body)
    assert response.status_code == 200, response.text
    assert seen["journey"].type == JOURNEY_TRANSCRIPTION
    assert seen["journey"].id == transcription_id


def test_rejected_proxy_upload_never_touches_another_journey(client, as_user):
    as_user(_make_user(roles=["Judge"]))
    victim = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    with capture_telemetry() as telemetry:
        # Wrong prefix (400) and someone else's folder (403).
        for key in (f"elsewhere/{victim}.mp4", f"user-uploads/someone-else/{victim}.mp4"):
            response = client.put("/api/upload-audio", params={"key": key}, content=b"audio")
            assert response.status_code in (400, 403)
        codes = [e[c.ERROR_CODE] for e in telemetry.events("upload.rejected")]
        assert codes == ["upload.invalid_key", "upload.forbidden"]
        assert all(e[c.OUTCOME] == "failure" for e in telemetry.events("upload.rejected"))
        assert not [e for e in telemetry.events() if e.get(c.JOURNEY_ID) == victim]
        assert not [s for s in telemetry.finished_spans() if s.name == "upload"]


def test_storage_failure_on_proxy_upload_is_an_error(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    tid = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    mocker.patch("transcribe_api.api.routes_dictation._validate_blob_ownership")
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_owner", return_value=None)
    manager = mocker.AsyncMock()
    manager.create_blob_from_bytes = mocker.AsyncMock(return_value=False)
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("transcribe_api.api.routes_dictation.AsyncAzureBlobManager", return_value=manager)
    with capture_telemetry() as telemetry:
        response = client.put("/api/upload-audio", params={"key": f"user-uploads/oid/{tid}.mp4"}, content=b"a")
        assert response.status_code == 500
        (failed,) = telemetry.events("upload.failed")
        assert failed[c.OUTCOME] == "error"
        assert failed[c.ERROR_CODE] == "storage.write_failed"
        assert failed[c.JOURNEY_ID] == tid


def test_upload_url_event_only_once_a_url_exists(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    settings = mocker.MagicMock(DIRECT_SDK_ACCESS=True, AZURE_STORAGE_CONTAINER_NAME="c", UPLOAD_URL_TTL_MINUTES=10)
    mocker.patch("transcribe_api.api.routes_dictation.get_settings", return_value=settings)
    mocker.patch(
        "transcribe_api.api.routes_dictation.generate_blob_upload_url", side_effect=RuntimeError("sas signing failed")
    )
    test_client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(
        client.app, raise_server_exceptions=False
    )
    with capture_telemetry() as telemetry:
        response = test_client.post("/api/get-upload-url", json={"file_extension": "mp4"})
        assert response.status_code == 500
        assert telemetry.events("upload.url_issued") == []


def test_submission_json_storage_failure_is_an_error_with_its_code(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    _patch_submission_deps(mocker)
    manager = mocker.AsyncMock()
    manager.create_blob_from_file = mocker.AsyncMock(return_value=False)
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("transcribe_api.api.routes_dictation.AsyncAzureBlobManager", return_value=manager)
    with capture_telemetry() as telemetry:
        response = client.post("/api/live-transcription/submission", json=_MINIMAL_BODY)
        assert response.status_code == 500
        (failed,) = telemetry.events("submission.failed")
        assert failed[c.OUTCOME] == "error"
        assert failed[c.ERROR_CODE] == "storage.write_failed"


def test_submission_unexpected_failure_keeps_its_type_and_no_premature_event(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    _patch_submission_deps(mocker)
    mocker.patch("transcribe_api.api.routes_dictation.save_transcription_job", side_effect=KeyError("db"))
    with capture_telemetry() as telemetry:
        response = client.post("/api/live-transcription/submission", json=_MINIMAL_BODY)
        assert response.status_code == 500
        (failed,) = telemetry.events("submission.failed")
        assert failed[c.ERROR_CODE] == "unhandled.KeyError"
        # One exception event, the real one (not the generic 500 that replaced it).
        (span,) = [s for s in telemetry.finished_spans() if s.name == "submission"]
        exceptions = [e for e in span.events if e.name == "exception"]
        assert [e.attributes["exception.type"] for e in exceptions] == ["KeyError"]
        # The transcription was never completed, so it was never announced as available.
        assert telemetry.events("transcript.available") == []


def test_submission_client_error_from_a_helper_is_a_handled_failure(client, as_user, mocker):
    from fastapi import HTTPException

    as_user(_make_user(roles=["Judge"]))
    _patch_submission_deps(mocker)
    mocker.patch(
        "transcribe_api.api.routes_dictation.save_transcription_job", side_effect=HTTPException(status_code=409)
    )
    with capture_telemetry() as telemetry:
        response = client.post("/api/live-transcription/submission", json=_MINIMAL_BODY)
        assert response.status_code == 409
        (failed,) = telemetry.events("submission.failed")
        assert failed[c.OUTCOME] == "failure"
        assert failed[c.ERROR_CODE] == "http.409"


def _proxy_upload(client, mocker, owner):
    mocker.patch("transcribe_api.api.routes_dictation._validate_blob_ownership")
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_owner", return_value=owner)
    manager = mocker.AsyncMock()
    manager.create_blob_from_bytes = mocker.AsyncMock(return_value=True)
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("transcribe_api.api.routes_dictation.AsyncAzureBlobManager", return_value=manager)
    tid = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    with capture_telemetry() as telemetry:
        response = client.put("/api/upload-audio", params={"key": f"user-uploads/oid/{tid}.mp4"}, content=b"a")
        assert response.status_code == 200
        return tid, telemetry.events()


def test_proxy_upload_naming_another_users_transcription_is_refused_outside_its_journey(client, as_user, mocker):
    # SEC-1: refused (403) and recorded as a rejection, never in that transcription's journey.
    as_user(_make_user(roles=["Judge"]))
    mocker.patch("transcribe_api.api.routes_dictation._validate_blob_ownership")
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_owner", return_value=uuid.uuid4())
    tid = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    with capture_telemetry() as telemetry:
        response = client.put("/api/upload-audio", params={"key": f"user-uploads/oid/{tid}.mp4"}, content=b"a")
        assert response.status_code == 403
        (rejected,) = telemetry.events("upload.rejected")
        assert rejected[c.ERROR_CODE] == "upload.forbidden"
        assert not [e for e in telemetry.events() if e.get(c.JOURNEY_ID) == tid]


def test_proxy_upload_of_own_or_new_transcription_is_in_its_journey(client, as_user, mocker):
    user = _make_user(roles=["Judge"])
    as_user(user)
    for owner in (None, user.id):
        tid, events = _proxy_upload(client, mocker, owner=owner)
        (done,) = [e for e in events if e[c.CUSTOM_EVENT_NAME] == "upload.completed"]
        assert done[c.JOURNEY_ID] == tid


def test_reentering_the_current_transcription_journey_is_a_no_op():
    from transcribe_api.runtime.telemetry import transcription_journey

    tid = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    with transcription_journey(tid) as outer, transcription_journey(tid.upper().replace("-", "")) as inner:
        assert inner is outer  # same transcription, any UUID spelling
    with transcription_journey(tid) as outer, transcription_journey(tid) as inner:
        assert inner is outer
        assert current_journey() is outer


def test_failed_owner_lookup_never_fails_the_upload(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    mocker.patch("transcribe_api.api.routes_dictation._validate_blob_ownership")
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_owner", side_effect=RuntimeError("db down"))
    manager = mocker.AsyncMock()
    manager.create_blob_from_bytes = mocker.AsyncMock(return_value=True)
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("transcribe_api.api.routes_dictation.AsyncAzureBlobManager", return_value=manager)
    tid = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    with capture_telemetry() as telemetry:
        response = client.put("/api/upload-audio", params={"key": f"user-uploads/oid/{tid}.mp4"}, content=b"a")
        assert response.status_code == 200
        manager.create_blob_from_bytes.assert_awaited_once()
        assert not [e for e in telemetry.events() if e.get(c.JOURNEY_ID) == tid]
