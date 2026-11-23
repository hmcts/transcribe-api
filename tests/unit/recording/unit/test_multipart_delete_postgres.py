"""DIAAT-408: the delete of a job in parts against a real Postgres (TEST_POSTGRES_URL, a throwaway database;
skipped without one). What SQLite cannot show: the lock the delete takes on the job row, the key-share lock a
part insert needs on it, and the foreign key error the worker gets once the delete commits."""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from tests.unit.recording.unit.async_upload_helpers import USER_ID, add_job
from transcribe_api.domain import interface_recording as jobs
from transcribe_api.domain.models_recording import SpeechBatchJob, User
from transcribe_api.stt import local_storage, part_submission
from transcribe_api.stt.part_submission import delete_job_and_parts, part_blob_name

WAIT = 30


@pytest.fixture
def engine(fresh_postgres, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "http://api.test")
    from transcribe_api.runtime import settings_recording

    settings_recording.get_settings.cache_clear()
    with Session(fresh_postgres) as session:
        session.add(User(id=USER_ID, azure_user_id="oid", email="judge@example.test", role="Judge"))
        session.commit()
    return fresh_postgres


def _plan(job, index=1, count=1):
    return jobs.PartPlan(index, count, 0.0, 10.0, "start", part_blob_name(job.audio_blob_path, index))


def test_a_part_inserted_while_the_delete_holds_the_job_waits_and_then_fails_its_foreign_key(engine):
    job = add_job(engine, source_blob_path=None)
    holding, release = threading.Event(), threading.Event()
    outcome: dict = {}

    def the_delete():
        with Session(engine) as session:
            session.execute(
                text("SELECT id FROM transcription_job WHERE id = :id FOR UPDATE"), {"id": job.id}
            )  # what delete_job_unless_parts_unreleased starts with
            holding.set()
            assert release.wait(WAIT)
            assert jobs.delete_job_unless_parts_unreleased(session, job.id) == []

    def the_worker():
        try:
            with Session(engine) as session:
                jobs.create_job_parts(session, job.id, [_plan(job)])
            outcome["inserted"] = True
        except IntegrityError as exc:
            outcome["error"] = exc

    deleting = threading.Thread(target=the_delete)
    deleting.start()
    assert holding.wait(WAIT)
    inserting = threading.Thread(target=the_worker)
    inserting.start()
    # Wait until Postgres says the insert is blocked on a lock (not for a fixed time): it is the lock on the job.
    blocked = threading.Event()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as watcher:  # no cached statistics
        for _ in range(WAIT * 20):
            waiting = watcher.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE '%INSERT INTO transcription_job_part%'"
                )
            ).scalar()
            if waiting:
                blocked.set()
                break
            blocked.wait(0.05)
    assert blocked.is_set(), "the part insert was not blocked by the lock on the job"
    assert "inserted" not in outcome and "error" not in outcome
    release.set()
    deleting.join(WAIT)
    inserting.join(WAIT)

    # Once the delete committed, the waiting insert failed its foreign key: "the job is gone".
    assert "inserted" not in outcome
    assert isinstance(outcome["error"], IntegrityError)
    assert outcome["error"].orig.pgcode == "23503"  # foreign_key_violation
    with Session(engine) as session:
        assert session.get(SpeechBatchJob, job.id) is None
        assert jobs.get_job_parts(session, job.id) == []


async def test_a_part_the_worker_records_after_the_cleanup_is_released_before_the_job_is_deleted(engine, mocker):
    job = add_job(engine, source_blob_path=None)
    deleted = []

    async def delete_batch_job(url):
        deleted.append(url)

    mocker.patch.object(part_submission, "delete_batch_job", new=delete_batch_job)
    real_cleanup = part_submission.cleanup_job_parts
    made = []

    async def cleanup_then_the_worker_records_a_part(*args, **kwargs):
        clean = await real_cleanup(*args, **kwargs)
        with Session(engine) as session:
            (row,) = jobs.create_job_parts(session, job.id, [_plan(job)])
            local_storage.save(b"part", row.blob_path)
            made.append(f"mock://batch-jobs/{uuid4()}")
            jobs.mark_part_submitted(session, row.id, "azure-1", made[0])
        return clean

    mocker.patch.object(part_submission, "cleanup_job_parts", cleanup_then_the_worker_records_a_part)

    assert await delete_job_and_parts(job.id, engine=engine) is True

    assert deleted == made
    with Session(engine) as session:
        assert session.get(SpeechBatchJob, job.id) is None
        assert jobs.get_job_parts(session, job.id) == []
    with pytest.raises(FileNotFoundError):
        local_storage.size(part_blob_name(job.audio_blob_path, 1))


def test_a_job_with_a_part_that_still_records_its_resources_is_not_deleted_and_nothing_is_lost(engine):
    job = add_job(engine, source_blob_path=None)
    with Session(engine) as session:
        jobs.create_job_parts(session, job.id, [_plan(job)])
    with Session(engine) as session:
        unreleased = jobs.delete_job_unless_parts_unreleased(session, job.id)
    assert [r.blob_path for r in unreleased] == [part_blob_name(job.audio_blob_path, 1)]
    with Session(engine) as session:
        assert session.get(SpeechBatchJob, job.id) is not None  # rolled back: the job and the row are still there
        assert len(jobs.get_job_parts(session, job.id)) == 1
