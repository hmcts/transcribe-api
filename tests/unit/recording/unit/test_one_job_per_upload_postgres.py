"""DIAAT-408: one live job per upload, with two requests at the same moment, on a real Postgres
(TEST_POSTGRES_URL, a throwaway database; skipped without one).

``POST /jobs`` looks for a live job from the upload and then inserts its own. Two requests could both look
before either inserted and both create one, and both be submitted to Azure and billed. The lookup is now
preceded by ``pg_advisory_xact_lock`` on (user, upload), held until the job row is committed.
"""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.unit.recording.unit.async_upload_helpers import USER_ID, tone
from transcribe_api.api import routes_recording
from transcribe_api.api.app import create_app
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.dependencies_recording import get_current_user
from transcribe_api.domain.models_recording import SpeechBatchJob, User
from transcribe_api.runtime import settings_dictation, settings_recording
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.stt import submission

WAIT = 30


@pytest.fixture(autouse=True)
def _no_rate_limit():
    from transcribe_api.api.routes_recording import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def engine(fresh_postgres, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "http://api.test")
    monkeypatch.setenv("SPEECH_BACKEND", "mock")
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", "false")  # converted in the request: the job is submitted at once
    settings_recording.get_settings.cache_clear()
    settings_dictation.get_settings.cache_clear()
    with Session(fresh_postgres) as session:
        session.add(User(id=USER_ID, azure_user_id="oid", email="judge@example.test", role="Judge"))
        session.commit()
    return fresh_postgres


@pytest.fixture
def make_client(engine):
    user = AuthenticatedUser(
        db_user=User(id=USER_ID, azure_user_id="oid", email="judge@example.test", role="Judge"),
        ctx=Context("IAC", "Judge"),
    )

    def make() -> TestClient:
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: user

        def session():
            with Session(engine) as s:
                yield s

        app.dependency_overrides[get_session] = session
        return TestClient(app, raise_server_exceptions=False)

    return make


@pytest.fixture
def submitted(monkeypatch):
    calls: list[str] = []

    async def submit(audio_sas_url, display_name, locale, enable_diarization):
        calls.append(display_name)
        return f"mock://batch-jobs/{uuid4()}"

    monkeypatch.setattr(submission, "submit_batch_job", submit)
    return calls


def _upload(client, mocker) -> dict:
    async def convert(input_path, wav_path):
        wav_path.write_bytes(b"WAV")

    mocker.patch("transcribe_api.api.routes_recording.normalize_to_wav", side_effect=convert)
    response = client.post("/api/v1/uploads", files={"file": ("hearing.mp3", tone(".mp3", 1.0), "audio/mpeg")})
    assert response.status_code == 201, response.text
    return response.json()


def _post_together(make_client, upload: dict, count: int = 2) -> list[int]:
    """``count`` requests for one upload at the same moment, each from its own client (its own event loop and thread)."""
    barrier = threading.Barrier(count)
    statuses: list[int] = []

    def post():
        client = make_client()
        barrier.wait(WAIT)
        response = client.post(
            "/api/v1/jobs",
            json={"audio_url": upload["audio_url"], "blob_name": upload["blob_name"], "metadata": {}},
        )
        statuses.append(response.status_code)

    threads = [threading.Thread(target=post) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT * 2)
    return sorted(statuses)


@pytest.mark.parametrize("flag_on", [False, True], ids=["submitted_at_once", "deferred"])
def test_two_requests_for_one_upload_at_the_same_moment_make_one_job(
    engine, make_client, submitted, mocker, monkeypatch, flag_on
):
    """Many repetitions: exactly one 201 and one 409, one job row per upload, Azure called once (not at all for a
    deferred job: the worker does that)."""
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", str(flag_on).lower())
    settings_dictation.get_settings.cache_clear()
    client = make_client()
    repetitions = 25 if not flag_on else 15
    for _ in range(repetitions):
        upload = _upload(client, mocker)
        before = len(submitted)

        assert _post_together(make_client, upload) == [201, 409]

        assert len(submitted) == before + (0 if flag_on else 1)
    with Session(engine) as session:
        assert len(session.exec(select(SpeechBatchJob)).all()) == repetitions


def test_the_lock_is_what_makes_it_so_both_lookups_are_forced_to_overlap(engine, make_client, submitted, mocker):
    """Each request is held after its lookup until the other has also looked (or 1.5 s have passed). Without the
    lock both pass the lookup and both create a job; with it the second cannot look until the first has committed,
    so the barrier times out for the first and one job is made."""
    real = routes_recording.has_live_job_for_upload
    meet = threading.Barrier(2)

    def look_then_wait_for_the_other(session, user_id, upload_id):
        found = real(session, user_id, upload_id)
        try:
            meet.wait(1.5)
        except threading.BrokenBarrierError:
            meet.reset()
        return found

    mocker.patch.object(routes_recording, "has_live_job_for_upload", look_then_wait_for_the_other)
    client = make_client()
    for _ in range(5):
        upload = _upload(client, mocker)
        assert _post_together(make_client, upload) == [201, 409]
    with Session(engine) as session:
        assert len(session.exec(select(SpeechBatchJob)).all()) == 5


def test_without_the_lock_the_same_overlap_makes_two_jobs(engine, make_client, submitted, mocker):
    """The mutation check: the lock taken out, the forced overlap gives two jobs for one upload."""
    mocker.patch.object(routes_recording, "lock_upload_for_submission", lambda *a, **k: None)
    real = routes_recording.has_live_job_for_upload
    meet = threading.Barrier(2)

    def look_then_wait_for_the_other(session, user_id, upload_id):
        found = real(session, user_id, upload_id)
        try:
            meet.wait(WAIT)
        except threading.BrokenBarrierError:
            meet.reset()
        return found

    mocker.patch.object(routes_recording, "has_live_job_for_upload", look_then_wait_for_the_other)
    upload = _upload(make_client(), mocker)
    assert _post_together(make_client, upload) == [201, 201]
    with Session(engine) as session:
        assert len(session.exec(select(SpeechBatchJob)).all()) == 2


def test_a_request_that_cannot_get_the_lock_in_time_is_refused_with_a_409_and_makes_no_job(
    engine, make_client, submitted, mocker
):
    from sqlalchemy import text

    from transcribe_api.domain import interface_recording

    mocker.patch.object(interface_recording, "UPLOAD_LOCK_TIMEOUT", "300ms")
    client = make_client()
    upload = _upload(client, mocker)
    upload_id = routes_recording._upload_uuid(upload["blob_name"])
    with engine.connect() as holder:  # another submission of the same upload, still in its transaction
        holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"{USER_ID}:{upload_id}"}
        )
        response = client.post(
            "/api/v1/jobs", json={"audio_url": upload["audio_url"], "blob_name": upload["blob_name"], "metadata": {}}
        )
        holder.rollback()
    assert response.status_code == 409
    assert response.json()["detail"] == "Concurrent submission conflict; retry"
    assert submitted == []
    with Session(engine) as session:
        assert session.exec(select(SpeechBatchJob)).all() == []
    # Once the other submission is over, the same request goes through.
    assert (
        client.post(
            "/api/v1/jobs", json={"audio_url": upload["audio_url"], "blob_name": upload["blob_name"], "metadata": {}}
        ).status_code
        == 201
    )
