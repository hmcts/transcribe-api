"""DIAAT-408: DELETE /jobs/{id} of a job prepared in parts removes the parts' blobs, their Azure jobs and
their rows, as well as the job's own WAV and its waiting original (the job in flight is covered in
tests/unit/recording/unit/audio/test_multipart_preparation.py)."""

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
from transcribe_api.domain.models_recording import SpeechBatchJob, User
from transcribe_api.runtime import settings_recording
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.stt import local_storage, part_submission
from transcribe_api.stt.part_submission import part_blob_name


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


def _job_in_parts(engine) -> tuple[SpeechBatchJob, list[str]]:
    upload_id = uuid4()
    job = add_job(engine, upload_id=upload_id, source_blob_path=f"incoming/{USER_ID}/{upload_id}.mp3")
    local_storage.save(b"original", job.source_blob_path)
    local_storage.save(b"full wav", job.audio_blob_path)
    blobs = [part_blob_name(job.audio_blob_path, i) for i in (1, 2, 3)]
    plans = [
        jobs.PartPlan(i, 3, (i - 1) * 100.0, 100.0, "start" if i == 1 else "silence", blobs[i - 1]) for i in (1, 2, 3)
    ]
    with Session(engine) as session:
        for part in jobs.create_job_parts(session, job.id, plans):
            local_storage.save(b"part", part.blob_path)
            jobs.mark_part_submitted(
                session, part.id, f"azure-{part.part_index}", f"https://azure.test/jobs/azure-{part.part_index}"
            )
    return job, blobs


def _exists(name: str) -> bool:
    try:
        local_storage.size(name)
    except FileNotFoundError:
        return False
    return True


def test_deleting_a_job_in_parts_removes_its_blobs_azure_jobs_and_rows(client, engine, mocker):
    job, blobs = _job_in_parts(engine)
    deleted = []

    async def delete_batch_job(url):
        deleted.append(url)

    mocker.patch.object(part_submission, "delete_batch_job", new=delete_batch_job)

    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 204

    assert sorted(deleted) == [f"https://azure.test/jobs/azure-{i}" for i in (1, 2, 3)]
    assert not any(_exists(name) for name in (*blobs, job.audio_blob_path, job.source_blob_path))
    with Session(engine) as session:
        assert session.get(SpeechBatchJob, job.id) is None
        assert jobs.get_job_parts(session, job.id) == []


def test_a_part_that_azure_cannot_delete_does_not_stop_the_job_being_deleted(client, engine, mocker):
    job, blobs = _job_in_parts(engine)

    async def refuse(url):
        raise RuntimeError("azure down")

    mocker.patch.object(part_submission, "delete_batch_job", new=refuse)
    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 204
    assert not any(_exists(name) for name in blobs)  # the blobs went whatever Azure said


def test_a_job_of_240_minutes_or_less_is_deleted_as_before_without_looking_for_parts(client, engine, mocker):
    job = add_job(engine, source_blob_path=None)
    local_storage.save(b"wav", job.audio_blob_path)
    cleanup = mocker.patch("transcribe_api.api.routes_recording.cleanup_job_parts")
    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 204
    cleanup.assert_not_called()
