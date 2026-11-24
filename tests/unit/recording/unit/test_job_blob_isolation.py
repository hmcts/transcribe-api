"""DIAAT-408: two jobs made from one upload must not share, overwrite or delete each other's blobs.

``POST /jobs`` names the upload by ``blob_name`` and nothing stopped a second job being made from it. The
blobs a job uses are named from the upload (``uploads/{user}/{upload}-{stem}.wav``, its parts
``...partNN.wav``, its original ``incoming/{user}/{upload}.ext``), so two jobs from one upload shared all of
them and one job's submission, failure, cleanup or delete acted on the other's. Real routes, worker and
storage (a local directory), SQLite for the job table, mock Speech.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.unit.recording.unit.async_upload_helpers import USER_ID, new_engine, tone, tone_and_silence
from transcribe_api.api.app import create_app
from transcribe_api.domain import interface_recording as jobs
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.dependencies_recording import get_current_user
from transcribe_api.domain.models_recording import JobStatus, SpeechBatchJob, User
from transcribe_api.runtime import settings_dictation, settings_recording
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import upload_id_from_blob
from transcribe_api.stt import local_storage, part_submission, recording_limits
from transcribe_api.stt import preparation_service as prep
from transcribe_api.stt.audio_storage import source_blob_name
from transcribe_api.stt.batch_client import BatchSubmissionError
from transcribe_api.stt.preparation_service import PreparationService
from transcribe_api.stt.submission import defer_batch_job

LIMIT = 30.0  # stands for Azure's 240 minutes
THREE_PARTS = (("t", 20.0), ("s", 2.0), ("t", 20.0), ("s", 2.0), ("t", 20.0))  # 64 s


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "http://api.test")
    monkeypatch.setenv("SPEECH_BACKEND", "mock")
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", "true")
    monkeypatch.setenv("LONG_RECORDINGS_ENABLED", "true")
    settings_recording.get_settings.cache_clear()
    settings_dictation.get_settings.cache_clear()
    settings = get_settings()
    mocker.patch.object(settings, "MAX_AUDIO_DURATION_SECONDS", 1000)
    mocker.patch.object(settings, "PART_TARGET_SECONDS", 22.0)
    mocker.patch.object(settings, "PART_RETRY_BACKOFF_SECONDS", 0)
    mocker.patch.object(settings, "PREPARE_RETRY_BACKOFF_SECONDS", 0)
    mocker.patch.object(settings, "PREPARE_MAX_CONCURRENT", 1)  # one job at a time: the order is the test's
    mocker.patch.object(prep, "AZURE_BATCH_FILE_LIMIT_SECONDS", LIMIT)
    mocker.patch.object(recording_limits, "AZURE_BATCH_FILE_LIMIT_SECONDS", LIMIT)


@pytest.fixture(autouse=True)
def _no_rate_limit():
    from transcribe_api.api.routes_recording import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def engine(mocker, tmp_path):
    engine = new_engine(tmp_path / "jobs.db")
    mocker.patch.object(prep, "get_engine", new=lambda: engine)
    mocker.patch.object(part_submission, "get_engine", new=lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine):
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


class Azure:
    """The mock Speech backend's submit and delete, recorded."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.deleted: list[str] = []
        self.fail_submission = False

    async def submit(self, audio_sas_url, display_name, locale, enable_diarization):
        self.submitted.append(display_name)
        if self.fail_submission:
            raise BatchSubmissionError("refused")
        return f"mock://batch-jobs/{uuid4()}"

    async def delete(self, job_url):
        self.deleted.append(job_url)


@pytest.fixture
def azure(mocker):
    from transcribe_api.stt import submission

    azure = Azure()
    mocker.patch.object(part_submission, "submit_batch_job", new=azure.submit)
    mocker.patch.object(submission, "submit_batch_job", new=azure.submit)
    mocker.patch.object(prep, "delete_batch_job", new=azure.delete)
    mocker.patch.object(part_submission, "delete_batch_job", new=azure.delete)
    return azure


def _exists(name: str | None) -> bool:
    if name is None:
        return False
    try:
        local_storage.size(name)
    except FileNotFoundError:
        return False
    return True


def _upload(client, flag_on: bool, monkeypatch, mocker) -> dict:
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", str(flag_on).lower())
    settings_dictation.get_settings.cache_clear()

    async def convert(input_path, wav_path):
        wav_path.write_bytes(b"WAV")

    mocker.patch("transcribe_api.api.routes_recording.normalize_to_wav", side_effect=convert)
    response = client.post("/api/v1/uploads", files={"file": ("hearing.mp3", tone(".mp3", 2.0), "audio/mpeg")})
    assert response.status_code == 201, response.text
    return response.json()


def _post_job(client, upload: dict, **extra):
    body = {"audio_url": upload["audio_url"], "blob_name": upload["blob_name"], "metadata": {}, **extra}
    return client.post("/api/v1/jobs", json=body)


def _rows(engine) -> list[SpeechBatchJob]:
    with Session(engine) as session:
        return list(session.exec(select(SpeechBatchJob)).all())


# What the routes allow ------------------------------------------------------------------------------


@pytest.mark.parametrize("flag_on", [False, True], ids=["converted_in_the_request", "deferred"])
def test_a_second_job_from_an_upload_that_has_a_live_job_is_refused(
    client, engine, azure, monkeypatch, mocker, flag_on
):
    upload = _upload(client, flag_on, monkeypatch, mocker)

    first = _post_job(client, upload)
    second = _post_job(client, upload)  # no idempotency key: a second, distinct job from the same upload

    assert first.status_code == 201, first.text
    assert second.status_code == 409
    assert "already" in second.json()["detail"]
    assert len(_rows(engine)) == 1


@pytest.mark.parametrize("flag_on", [False, True], ids=["converted_in_the_request", "deferred"])
def test_an_idempotent_resubmission_is_still_the_same_job_not_a_refusal(
    client, engine, azure, monkeypatch, mocker, flag_on
):
    upload = _upload(client, flag_on, monkeypatch, mocker)
    first = _post_job(client, upload, idempotency_key="k1")
    again = _post_job(client, upload, idempotency_key="k1")
    assert first.status_code == again.status_code == 201
    assert first.json()["job_id"] == again.json()["job_id"]


def test_a_failed_job_does_not_stop_the_upload_being_submitted_again(client, engine, azure, monkeypatch, mocker):
    """A retry of a job that failed (here: Azure refused it) from the same upload still works."""
    upload = _upload(client, False, monkeypatch, mocker)
    azure.fail_submission = True
    assert _post_job(client, upload).status_code in (400, 502, 503)  # the job is FAILED
    (failed,) = _rows(engine)
    assert failed.status == JobStatus.FAILED

    azure.fail_submission = False
    retry = _post_job(client, upload)

    assert retry.status_code == 201, retry.text
    assert len(_rows(engine)) == 2


def test_deleting_a_failed_job_leaves_the_audio_of_its_retry(client, engine, azure, monkeypatch, mocker):
    """The failed job and its retry from one converted upload share its WAV: deleting the failed one must not take it."""
    upload = _upload(client, False, monkeypatch, mocker)
    azure.fail_submission = True
    _post_job(client, upload)
    azure.fail_submission = False
    retry = _post_job(client, upload)
    (failed,) = [r for r in _rows(engine) if r.status == JobStatus.FAILED]
    assert _exists(upload["blob_name"])

    assert client.delete(f"/api/v1/jobs/{failed.id}").status_code == 204

    assert _exists(upload["blob_name"])  # the retry's audio is still there
    assert client.get(f"/api/v1/jobs/{retry.json()['job_id']}/audio", headers={"Range": "bytes=0-1"}).status_code in (
        200,
        206,
    )


# What the blobs are called ---------------------------------------------------------------------------


def _deferred(engine, upload_id: UUID, *, original: bytes | None = None) -> SpeechBatchJob:
    """A deferred job from the upload, as POST /jobs makes it (the original is stored once per upload)."""
    source = source_blob_name(USER_ID, upload_id, ".wav")
    if not _exists(source):
        local_storage.save(original if original is not None else tone(".wav", 3.0), source)
    with Session(engine) as session:
        return defer_batch_job(
            session,
            "https://client.example/ignored.wav",
            source,
            audio_blob_path=f"uploads/{USER_ID}/{upload_id}-hearing.wav",
            user_id=USER_ID,
        )


def test_two_deferred_jobs_from_one_upload_never_share_a_blob_name(engine):
    upload_id = uuid4()
    first, second = _deferred(engine, upload_id), _deferred(engine, upload_id)

    assert first.audio_blob_path != second.audio_blob_path
    for job in (first, second):
        assert job.audio_blob_path.startswith(f"uploads/{USER_ID}/")  # the ownership check still holds
        assert upload_id_from_blob(job.audio_blob_path) == str(upload_id)  # the upload's journey is still found
        assert str(job.id) in job.audio_blob_path  # derived from the job's own id
        assert job.audio_blob_path.endswith("-hearing.wav")
    names = {
        part_submission.part_blob_name(job.audio_blob_path, index) for job in (first, second) for index in (1, 2, 3)
    }
    assert len(names) == 6  # no part blob is shared either


def test_two_deferred_jobs_from_one_upload_are_each_prepared_and_the_original_waits_for_both(engine, azure):
    upload_id = uuid4()
    first, second = _deferred(engine, upload_id), _deferred(engine, upload_id)
    source = first.source_blob_path

    async def run():
        service = PreparationService()
        await service.poll_once()
        await service.wait_idle()
        # One job at a time: the first is done; the second still needs the original.
        assert _exists(source)
        await service.poll_once()
        await service.wait_idle()

    asyncio.run(run())

    with Session(engine) as session:
        rows = [session.get(SpeechBatchJob, j.id) for j in (first, second)]
    assert [r.status for r in rows] == [JobStatus.SUBMITTED, JobStatus.SUBMITTED]
    assert all(_exists(r.audio_blob_path) for r in rows) and rows[0].audio_blob_path != rows[1].audio_blob_path
    assert not _exists(source)  # removed once nobody needs it


def test_deleting_a_job_leaves_the_original_another_job_still_needs(client, engine):
    upload_id = uuid4()
    first, second = _deferred(engine, upload_id), _deferred(engine, upload_id)

    assert client.delete(f"/api/v1/jobs/{first.id}").status_code == 204
    assert _exists(first.source_blob_path)  # the second job is still waiting for it

    assert client.delete(f"/api/v1/jobs/{second.id}").status_code == 204
    assert not _exists(first.source_blob_path)


def _in_parts(engine, azure) -> tuple[SpeechBatchJob, SpeechBatchJob]:
    upload_id = uuid4()
    first = _deferred(engine, upload_id, original=tone_and_silence(THREE_PARTS))
    second = _deferred(engine, upload_id)

    async def run():
        service = PreparationService()
        for _ in range(2):  # one job at a time
            await service.poll_once()
            await service.wait_idle()

    asyncio.run(run())
    return first, second


def _part_blobs(engine, job: SpeechBatchJob) -> list[str]:
    with Session(engine) as session:
        return [p.blob_path for p in jobs.get_job_parts(session, job.id)]


def test_deleting_one_job_in_parts_leaves_the_blobs_parts_and_azure_jobs_of_the_other(client, engine, azure):
    first, second = _in_parts(engine, azure)
    mine, theirs = _part_blobs(engine, first), _part_blobs(engine, second)
    assert len(mine) == len(theirs) == 3 and not set(mine) & set(theirs)
    with Session(engine) as session:
        theirs_urls = [p.batch_job_url for p in jobs.get_job_parts(session, second.id)]
        wav = session.get(SpeechBatchJob, second.id).audio_blob_path

    assert client.delete(f"/api/v1/jobs/{first.id}").status_code == 204

    assert not any(_exists(n) for n in mine)
    assert all(_exists(n) for n in theirs) and _exists(wav)
    assert not set(azure.deleted) & set(theirs_urls)  # the other job's Azure jobs were not touched
    assert len(_part_blobs(engine, second)) == 3


def test_cleaning_up_one_job_in_parts_leaves_the_blobs_of_the_other(engine, azure):
    first, second = _in_parts(engine, azure)
    theirs = _part_blobs(engine, second)

    assert (
        asyncio.run(part_submission.cleanup_job_parts(first.id, engine=engine)) is True
    )  # as when it finishes or fails

    assert not any(_exists(n) for n in _part_blobs(engine, first) if n)
    assert all(_exists(n) for n in theirs)


def test_a_part_row_naming_a_blob_that_is_not_derived_from_its_job_is_never_deleted(engine, azure):
    """Cleanup deletes only blobs derived from the job's own WAV name (which has its id in it): a part row that
    records another job's blob (damaged, or made before the names were per job) does not make it the job's."""
    upload_id = uuid4()
    mine, other = _deferred(engine, upload_id), _deferred(engine, upload_id)
    others_blob = part_submission.part_blob_name(other.audio_blob_path, 1)
    local_storage.save(b"the other job's part", others_blob)
    with Session(engine) as session:
        jobs.create_job_parts(session, mine.id, [jobs.PartPlan(1, 1, 0.0, 10.0, "start", others_blob)])

    assert asyncio.run(part_submission.cleanup_job_parts(mine.id, engine=engine)) is True

    assert _exists(others_blob)


def test_the_upload_lock_does_nothing_on_a_database_that_is_not_postgres(engine):
    """SQLite (these tests) has no advisory locks: the call is a no-op and touches nothing."""
    with Session(engine) as session:
        jobs.lock_upload_for_submission(session, USER_ID, uuid4())
        assert not session.in_transaction()  # no statement was run
