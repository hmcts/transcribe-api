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


def _rows(engine, job_id):
    with Session(engine) as session:
        return jobs.get_job_parts(session, job_id)


def test_a_part_that_azure_cannot_delete_keeps_the_job_and_its_rows_and_the_delete_can_be_repeated(
    client, engine, mocker
):
    """A failed delete must not lose the only record of what is left: 502, the job and the part rows (with
    their Azure URLs) stay, the job is marked, and once Azure answers a second delete finishes the job."""
    job, blobs = _job_in_parts(engine)

    async def refuse(url):
        raise RuntimeError("azure down")

    mocker.patch.object(part_submission, "delete_batch_job", new=refuse)
    response = client.delete(f"/api/v1/jobs/{job.id}")

    assert response.status_code == 502
    assert not any(_exists(name) for name in blobs)  # each step is tried on its own: the blobs went
    with Session(engine) as session:
        kept = session.get(SpeechBatchJob, job.id)
        assert kept is not None and kept.needs_cleanup is True
    assert [p.batch_job_url for p in _rows(engine, job.id)] == [f"https://azure.test/jobs/azure-{i}" for i in (1, 2, 3)]

    deleted = []

    async def delete_batch_job(url):
        deleted.append(url)

    mocker.patch.object(part_submission, "delete_batch_job", new=delete_batch_job)
    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 204
    assert sorted(deleted) == [f"https://azure.test/jobs/azure-{i}" for i in (1, 2, 3)]
    with Session(engine) as session:
        assert session.get(SpeechBatchJob, job.id) is None
    assert _rows(engine, job.id) == []


def _storage_refusing(mocker, name_part: str, times: int = 1):
    """The local storage, refusing to delete a blob whose name contains ``name_part`` (``times`` times)."""
    from transcribe_api.stt.audio_storage import LocalAudioStorage, StorageError

    real = LocalAudioStorage.delete
    refused = []

    async def delete(self, blob_name):
        if name_part in blob_name and len(refused) < times:
            refused.append(blob_name)
            raise StorageError("storage down")
        await real(self, blob_name)

    mocker.patch.object(LocalAudioStorage, "delete", delete)
    return refused


def test_a_part_blob_that_cannot_be_deleted_keeps_the_job_and_its_rows_and_a_second_delete_succeeds(
    client, engine, mocker
):
    job, blobs = _job_in_parts(engine)
    refused = _storage_refusing(mocker, ".part02.")
    deleted = []

    async def delete_batch_job(url):
        deleted.append(url)

    mocker.patch.object(part_submission, "delete_batch_job", new=delete_batch_job)

    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 502

    assert refused == [blobs[1]]
    rows = _rows(engine, job.id)
    assert len(rows) == 3
    # What was deleted is forgotten; what is left is still recorded, so the retry knows.
    assert [p.blob_path for p in rows] == [None, blobs[1], None]
    assert _exists(blobs[1]) and not _exists(blobs[0]) and not _exists(blobs[2])
    with Session(engine) as session:
        kept = session.get(SpeechBatchJob, job.id)
        assert kept is not None and kept.needs_cleanup is True

    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 204
    assert not any(_exists(name) for name in blobs)
    assert _rows(engine, job.id) == []
    with Session(engine) as session:
        assert session.get(SpeechBatchJob, job.id) is None


def test_an_original_that_cannot_be_deleted_keeps_the_job_and_a_retry_succeeds(client, engine, mocker):
    job, _blobs = _job_in_parts(engine)
    mocker.patch.object(part_submission, "delete_batch_job", new=mocker.AsyncMock())
    refused = _storage_refusing(mocker, "incoming/")

    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 502

    assert refused == [job.source_blob_path]
    assert _exists(job.source_blob_path)
    with Session(engine) as session:
        assert session.get(SpeechBatchJob, job.id) is not None
    assert len(_rows(engine, job.id)) == 3  # nothing was touched past the original

    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 204
    assert not _exists(job.source_blob_path)
    with Session(engine) as session:
        assert session.get(SpeechBatchJob, job.id) is None


def test_a_job_whose_original_is_already_gone_is_deleted(client, engine, mocker):
    """A blob that is not there is success: the original of a job that was prepared and settled."""
    job = add_job(engine, source_blob_path=f"incoming/{USER_ID}/{uuid4()}.mp3")
    local_storage.save(b"wav", job.audio_blob_path)
    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 204
    with Session(engine) as session:
        assert session.get(SpeechBatchJob, job.id) is None


def test_a_job_of_240_minutes_or_less_is_deleted_as_before_without_looking_for_parts(client, engine, mocker):
    job = add_job(engine, source_blob_path=None)
    local_storage.save(b"wav", job.audio_blob_path)
    cleanup = mocker.patch("transcribe_api.api.routes_recording.delete_job_and_parts")
    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 204
    cleanup.assert_not_called()
