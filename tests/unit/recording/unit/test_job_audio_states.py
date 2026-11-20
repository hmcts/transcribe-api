"""DIAAT-408: ``GET /jobs/{id}/audio`` for a job whose recording is still being prepared, and for a recording in parts.

A job waiting for the worker has no WAV yet: the answer is a 404 that says why (the job page never asks for
audio until the transcript is ready, so no player is broken). A recording in parts plays from its full WAV,
which is stored beside the parts and kept when they are deleted."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.unit.recording.unit.async_upload_helpers import USER_ID, add_job, new_engine
from transcribe_api.api.app import create_app
from transcribe_api.domain import interface_recording as jobs
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.dependencies_recording import get_current_user
from transcribe_api.domain.models_recording import JobStatus, User
from transcribe_api.runtime import settings_recording
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.stt import local_storage


@pytest.fixture
def engine(tmp_path):
    engine = new_engine(tmp_path / "jobs.db")
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "http://api.test")
    settings_recording.get_settings.cache_clear()
    app = create_app()
    user = AuthenticatedUser(
        db_user=User(id=USER_ID, azure_user_id="oid", email="judge@example.test", role="Judge"),
        ctx=Context("IAC", "Judge"),
    )
    app.dependency_overrides[get_current_user] = lambda: user

    def session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = session
    return TestClient(app, raise_server_exceptions=False)


def test_a_job_waiting_to_be_prepared_answers_404_and_says_the_recording_is_still_being_prepared(client, engine):
    job = add_job(engine, status=JobStatus.PENDING)  # a deferred job: its original is stored, its WAV is not yet

    response = client.get(f"/api/v1/jobs/{job.id}/audio")

    assert response.status_code == 404
    assert response.json()["detail"] == "The audio is not ready yet: the recording is still being prepared"


def test_a_missing_wav_of_any_other_job_is_still_just_not_found(client, engine):
    job = add_job(engine, status=JobStatus.SUCCEEDED, source_blob_path=None)
    response = client.get(f"/api/v1/jobs/{job.id}/audio")
    assert response.status_code == 404 and response.json()["detail"] == "Audio file not found"


def test_a_pending_job_whose_wav_is_already_stored_plays(client, engine):
    """Between the WAV being stored and the job being submitted the audio is there: it is served."""
    job = add_job(engine, status=JobStatus.PENDING)
    local_storage.save(b"RIFF" + b"x" * 100, job.audio_blob_path)
    response = client.get(f"/api/v1/jobs/{job.id}/audio")
    assert response.status_code == 200 and len(response.content) == 104


@pytest.mark.parametrize("status", [JobStatus.SUBMITTED, JobStatus.RUNNING, JobStatus.SUCCEEDED])
def test_a_recording_in_parts_plays_from_its_full_wav_with_ranges_in_every_state(client, engine, status):
    job = add_job(engine, status=status, source_blob_path=f"incoming/{uuid4()}.wav")
    wav = bytes(range(256)) * 4
    local_storage.save(wav, job.audio_blob_path)
    with Session(engine) as session:
        jobs.create_job_parts(session, job.id, [jobs.PartPlan(1, 1, 0.0, 10.0, "start", "uploads/p1")])

    whole = client.get(f"/api/v1/jobs/{job.id}/audio")
    ranged = client.get(f"/api/v1/jobs/{job.id}/audio", headers={"Range": "bytes=256-511"})

    assert whole.status_code == 200 and whole.content == wav
    assert ranged.status_code == 206 and ranged.content == wav[256:512]
    assert ranged.headers["content-range"] == f"bytes 256-511/{len(wav)}"
