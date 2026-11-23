"""DIAAT-408: POST /uploads and POST /jobs with ``async_upload_preparation`` on.

The request must not convert the audio (that is what held a 4-hour upload past the gateway's 30 s),
so these tests make ``normalize_to_wav`` fail the test if it is called, and time every request.
Real ffprobe and real small audio files; storage is the local directory backend; the job table is
SQLite. With the flag off the existing upload and job tests apply unchanged.
"""

from __future__ import annotations

import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry
from sqlmodel import Session, select

from tests.unit.recording.unit.async_upload_helpers import USER_ID, add_job, new_engine, tone
from transcribe_api.api.app import create_app
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.dependencies_recording import get_current_user
from transcribe_api.domain.models_recording import JobStatus, SpeechBatchJob, User
from transcribe_api.runtime import settings_dictation
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import configure_telemetry
from transcribe_api.stt import audio_storage, local_storage, submission
from transcribe_api.stt.audio_storage import source_blob_name

# What a request may take: far under the gateway's 30 s (the brief: well under 10 s).
FAST = 5.0


@pytest.fixture(autouse=True)
def _no_rate_limit():
    from transcribe_api.api.routes_recording import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def local_storage_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "http://api.test")
    from transcribe_api.runtime import settings_recording

    settings_recording.get_settings.cache_clear()
    return tmp_path / "audio"


def _flag(monkeypatch, on: bool, *, long_recordings: bool = False) -> None:
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", str(on).lower())
    monkeypatch.setenv("LONG_RECORDINGS_ENABLED", str(long_recordings).lower())
    settings_dictation.get_settings.cache_clear()


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


@pytest.fixture
def normalize(mocker):
    """The conversion, which the request must never run."""
    return mocker.patch(
        "transcribe_api.api.routes_recording.normalize_to_wav", side_effect=AssertionError("converted in the request")
    )


@pytest.fixture
def engine(tmp_path):
    engine = new_engine(tmp_path / "jobs.db")
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


def _upload(client, name="hearing.mp3", body=None, extension_seconds=3.0):
    body = tone(".mp3", extension_seconds) if body is None else body
    return client.post("/api/v1/uploads", files={"file": (name, body, "audio/mpeg")})


def _upload_id(blob_name: str) -> UUID:
    return UUID(blob_name.split("/")[2][:36])


# POST /uploads ---------------------------------------------------------------------------------


def test_the_upload_stores_the_original_and_never_converts_it(client, monkeypatch, normalize, local_storage_dir):
    _flag(monkeypatch, True)
    body = tone(".mp3", 3.0)

    started = time.perf_counter()
    response = _upload(client, body=body)
    elapsed = time.perf_counter() - started

    assert response.status_code == 201
    assert elapsed < FAST
    normalize.assert_not_called()
    blob_name = response.json()["blob_name"]
    # The response is the one a converting upload gives: the WAV's future name, under uploads/{user}/.
    assert blob_name.startswith(f"uploads/{USER_ID}/") and blob_name.endswith("-hearing.wav")
    assert "/api/v1/local-audio/" in response.json()["audio_url"]
    # The original is waiting under incoming/ as it was sent; there is no WAV yet.
    source = source_blob_name(USER_ID, _upload_id(blob_name), ".mp3")
    assert local_storage.read(source) == body
    with pytest.raises(FileNotFoundError):
        local_storage.size(blob_name)


def test_the_request_does_not_wait_for_a_slow_conversion(client, monkeypatch, mocker):
    """Even if the conversion took half a minute, the request would not notice: it is never started."""
    _flag(monkeypatch, True)

    async def half_a_minute(input_path, wav_path):
        import asyncio

        await asyncio.sleep(30)

    converter = mocker.patch("transcribe_api.api.routes_recording.normalize_to_wav", side_effect=half_a_minute)
    started = time.perf_counter()
    assert _upload(client, extension_seconds=5.0).status_code == 201
    assert time.perf_counter() - started < FAST
    converter.assert_not_called()


def test_with_the_flag_off_the_upload_still_converts_in_the_request(client, monkeypatch, mocker, local_storage_dir):
    _flag(monkeypatch, False)

    async def convert(input_path, wav_path):
        wav_path.write_bytes(b"WAV")

    converter = mocker.patch("transcribe_api.api.routes_recording.normalize_to_wav", side_effect=convert)
    response = _upload(client)
    assert response.status_code == 201
    converter.assert_called_once()
    local_storage.read(response.json()["blob_name"])  # the WAV is stored now
    with pytest.raises(FileNotFoundError):
        local_storage.size(source_blob_name(USER_ID, _upload_id(response.json()["blob_name"]), ".mp3"))


@pytest.mark.parametrize("body", [b"this is not audio " * 50, b"\x00" * 4096])
def test_a_file_that_is_not_audio_is_refused_in_the_request_and_stores_nothing(
    client, monkeypatch, normalize, tel, local_storage_dir, body
):
    _flag(monkeypatch, True)
    started = time.perf_counter()
    response = _upload(client, "bad.mp3", body)
    assert time.perf_counter() - started < FAST
    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "The audio file could not be decoded. It may be corrupt or in an unsupported format."
    )
    (invalid,) = tel.events("upload.validation_failed")
    assert invalid[c.ERROR_CODE] == "upload.undecodable"
    assert not local_storage_dir.exists() or not any(local_storage_dir.iterdir())
    normalize.assert_not_called()


def test_a_recording_over_the_limit_is_refused_with_the_sentence_before_anything_is_stored(
    client, monkeypatch, normalize, tel, local_storage_dir, mocker
):
    _flag(monkeypatch, True)
    mocker.patch.object(get_settings(), "MAX_AUDIO_DURATION_SECONDS", 60)  # 4 hours, scaled down
    started = time.perf_counter()
    response = _upload(client, extension_seconds=125.0)
    assert time.perf_counter() - started < FAST
    assert response.status_code == 422
    assert response.json()["detail"] == "The recording is 2 minutes long. Recordings can be at most 1 minute."
    (failed,) = tel.events("upload.failed")
    assert failed[c.ERROR_CODE] == "upload.too_long"
    assert failed["audio.duration_seconds"] == pytest.approx(125.0, abs=1.0)
    assert not local_storage_dir.exists() or not any(local_storage_dir.iterdir())
    normalize.assert_not_called()


def test_the_message_for_a_long_recording_names_the_effective_limit(client, monkeypatch, normalize, mocker):
    _flag(monkeypatch, True)  # long recordings still off: the limit is 4 hours
    mocker.patch(
        "transcribe_api.api.routes_recording.probe_duration_seconds",
        new=mocker.AsyncMock(return_value=4 * 3600 + 120.0),
    )
    response = _upload(client)
    assert response.status_code == 422
    assert response.json()["detail"] == "The recording is 4 hours 2 minutes long. Recordings can be at most 4 hours."


def test_with_both_flags_on_the_same_recording_is_accepted_up_to_8_hours(client, monkeypatch, normalize, mocker):
    _flag(monkeypatch, True, long_recordings=True)
    probe = mocker.patch(
        "transcribe_api.api.routes_recording.probe_duration_seconds",
        new=mocker.AsyncMock(return_value=4 * 3600 + 120.0),
    )
    assert _upload(client).status_code == 201
    probe.return_value = 8 * 3600 + 120.0
    response = _upload(client)
    assert response.status_code == 422
    assert response.json()["detail"] == "The recording is 8 hours 2 minutes long. Recordings can be at most 8 hours."


def test_a_recording_exactly_at_the_limit_is_accepted(client, monkeypatch, normalize, mocker):
    _flag(monkeypatch, True)
    mocker.patch(
        "transcribe_api.api.routes_recording.probe_duration_seconds", new=mocker.AsyncMock(return_value=4 * 3600.0)
    )
    assert _upload(client).status_code == 201


def test_a_file_that_states_no_length_is_accepted_and_the_exact_check_is_left_to_the_worker(
    client, monkeypatch, normalize, mocker, tel
):
    _flag(monkeypatch, True)
    mocker.patch("transcribe_api.api.routes_recording.probe_duration_seconds", new=mocker.AsyncMock(return_value=None))
    assert _upload(client).status_code == 201
    (done,) = tel.events("upload.completed")
    assert "audio.duration_seconds" not in done


def test_the_probed_length_is_on_the_upload_events(client, monkeypatch, normalize, tel):
    _flag(monkeypatch, True)
    assert _upload(client, extension_seconds=3.0).status_code == 201
    (done,) = tel.events("upload.completed")
    assert done["audio.duration_seconds"] == pytest.approx(3.0, abs=0.3)
    assert done["file.size_bytes"] > 0


def test_a_missing_ffprobe_is_a_503_not_a_bad_file(client, monkeypatch, normalize, mocker, tel):
    _flag(monkeypatch, True)
    mocker.patch(
        "transcribe_api.api.routes_recording.probe_duration_seconds",
        new=mocker.AsyncMock(side_effect=FileNotFoundError(2, "No such file or directory", "ffprobe")),
    )
    response = _upload(client)
    assert response.status_code == 503
    (failed,) = tel.events("upload.failed")
    assert failed[c.OUTCOME] == "error" and failed[c.ERROR_CODE] == "upload.transcoder_unavailable"


def test_the_size_backstop_and_the_file_type_checks_apply_as_before(client, monkeypatch, normalize, mocker, tel):
    _flag(monkeypatch, True)
    assert _upload(client, "notes.txt", b"hello").status_code == 422
    mocker.patch.object(get_settings(), "MAX_UPLOAD_BYTES", 10)
    response = _upload(client)
    assert response.status_code == 413
    assert [e[c.ERROR_CODE] for e in tel.events("upload.validation_failed")] == [
        "upload.unsupported_format",
        "upload.too_large",
    ]


def test_a_storage_failure_is_a_502_and_an_error(client, monkeypatch, normalize, mocker, tel):
    _flag(monkeypatch, True)
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "azure")
    get_settings.cache_clear()
    manager = mocker.AsyncMock()
    manager.create_blob_from_file = mocker.AsyncMock(return_value=False)
    manager.build_blob_url = mocker.Mock(return_value="https://x/y.wav")
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch.object(audio_storage, "AsyncAzureBlobManager", return_value=manager)

    response = _upload(client)

    assert response.status_code == 502
    (failed,) = tel.events("upload.failed")
    assert failed[c.OUTCOME] == "error" and failed[c.ERROR_CODE] == "storage.write_failed"
    # The blocks are sent in parallel, as set (memory: that many 4 MiB blocks).
    assert (
        manager.create_blob_from_file.call_args.kwargs["max_concurrency"] == get_settings().BLOB_UPLOAD_CONCURRENCY == 8
    )


def test_the_azure_upload_stores_under_incoming_and_answers_with_the_wavs_future_url(
    client, monkeypatch, normalize, mocker
):
    _flag(monkeypatch, True)
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "azure")
    get_settings.cache_clear()
    stored: dict[str, bytes] = {}
    manager = mocker.AsyncMock()

    async def create(path, blob_name, *args, **kwargs):
        stored[blob_name] = path.read_bytes()
        return True

    manager.create_blob_from_file = mocker.AsyncMock(side_effect=create)
    manager.build_blob_url = mocker.Mock(side_effect=lambda name: f"https://account.blob.example/c/{name}")
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch.object(audio_storage, "AsyncAzureBlobManager", return_value=manager)
    body = tone(".mp3", 2.0)

    response = _upload(client, body=body)

    assert response.status_code == 201
    blob_name = response.json()["blob_name"]
    assert list(stored) == [f"incoming/{USER_ID}/{_upload_id(blob_name)}.mp3"]
    assert stored[next(iter(stored))] == body
    assert response.json()["audio_url"] == f"https://account.blob.example/c/{blob_name}"


# POST /jobs ------------------------------------------------------------------------------------


def _store_original(upload_id: UUID, body: bytes = b"original", extension: str = ".mp3") -> str:
    name = source_blob_name(USER_ID, upload_id, extension)
    local_storage.save(body, name)
    return name


def _job_body(upload_id: UUID, **extra) -> dict:
    return {
        "audio_url": "https://client.example/anything.wav",
        "blob_name": f"uploads/{USER_ID}/{upload_id}-hearing.wav",
        "metadata": {"audio_file_name": "hearing.mp3"},
        **extra,
    }


@pytest.fixture
def no_azure(mocker):
    """Azure Speech must not be called by the request."""
    return mocker.patch.object(
        submission, "submit_batch_job", side_effect=AssertionError("Azure was called in the request")
    )


def test_the_job_is_created_pending_with_its_original_and_azure_is_not_called(
    client, engine, monkeypatch, no_azure, tel
):
    _flag(monkeypatch, True)
    upload_id = uuid4()
    source = _store_original(upload_id)

    started = time.perf_counter()
    response = client.post("/api/v1/jobs", json=_job_body(upload_id, audio_duration_seconds=12.5))
    assert time.perf_counter() - started < FAST

    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    no_azure.assert_not_called()
    with Session(engine) as session:
        (job,) = session.exec(select(SpeechBatchJob)).all()
    assert job.status == JobStatus.PENDING
    assert job.source_blob_path == source
    assert job.audio_blob_path == f"uploads/{USER_ID}/{upload_id}-hearing.wav"
    assert job.batch_job_id is None and job.batch_job_url is None and job.batch_job_status is None
    assert job.preparation_attempts == 0
    assert job.user_id == USER_ID and job.audio_duration_seconds == 12.5
    # job.created once, on the job's journey, and nothing from Azure.
    assert [e[c.JOURNEY_ID] for e in tel.events("job.created")] == [str(job.id)]
    assert tel.events("job.queued") == [] and tel.events("speech_submission.started") == []


def test_the_source_is_found_by_the_users_and_uploads_ids_not_by_anything_the_client_sends(
    client, engine, monkeypatch, mocker
):
    _flag(monkeypatch, True)
    upload_id = uuid4()
    other_users_original = f"incoming/{uuid4()}/{upload_id}.mp3"
    local_storage.save(b"x", other_users_original)
    submit = mocker.patch(
        "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
        new=mocker.AsyncMock(return_value=add_job(engine, source_blob_path=None)),
    )
    # Nothing under this user's prefix: not deferred, whatever the other user has stored and the
    # client says (metadata is stored as given and never read as a blob name).
    response = client.post(
        "/api/v1/jobs", json=_job_body(upload_id, metadata={"source_blob_path": other_users_original})
    )
    assert response.status_code == 201
    submit.assert_called_once()


def test_an_upload_with_no_original_is_submitted_at_once_as_before(client, engine, monkeypatch, mocker):
    """An upload made before the flag was turned on (its WAV is already stored): the old path."""
    _flag(monkeypatch, True)
    upload_id = uuid4()
    submit = mocker.patch(
        "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
        new=mocker.AsyncMock(return_value=add_job(engine, source_blob_path=None)),
    )
    response = client.post("/api/v1/jobs", json=_job_body(upload_id))
    assert response.status_code == 201
    submit.assert_called_once()
    assert submit.call_args.kwargs["audio_blob_path"] == f"uploads/{USER_ID}/{upload_id}-hearing.wav"


def test_with_the_flag_off_a_waiting_original_still_defers_the_job_and_azure_is_not_called(
    client, engine, monkeypatch, no_azure
):
    """An upload made while the flag was on has no WAV: a rollback must not send Azure a missing file."""
    _flag(monkeypatch, False)
    upload_id = uuid4()
    source = _store_original(upload_id)

    response = client.post("/api/v1/jobs", json=_job_body(upload_id))

    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    no_azure.assert_not_called()
    with Session(engine) as session:
        (job,) = session.exec(select(SpeechBatchJob)).all()
    assert job.status == JobStatus.PENDING and job.source_blob_path == source
    assert job.audio_blob_path == f"uploads/{USER_ID}/{upload_id}-hearing.wav"


def test_with_the_flag_off_and_no_original_the_job_is_submitted_as_before(client, engine, monkeypatch, mocker):
    _flag(monkeypatch, False)
    upload_id = uuid4()
    submit = mocker.patch(
        "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
        new=mocker.AsyncMock(return_value=add_job(engine, source_blob_path=None)),
    )
    assert client.post("/api/v1/jobs", json=_job_body(upload_id)).status_code == 201
    submit.assert_called_once()
    assert submit.call_args.kwargs["audio_blob_path"] == f"uploads/{USER_ID}/{upload_id}-hearing.wav"
    with Session(engine) as session:
        assert session.exec(select(SpeechBatchJob).where(SpeechBatchJob.source_blob_path.is_not(None))).all() == []


def test_with_the_flag_off_a_failed_lookup_is_logged_and_the_job_is_submitted_as_before(
    client, engine, monkeypatch, mocker, caplog
):
    """With the flag off the lookup failing is not a reason to fail the request: as before, no original."""
    _flag(monkeypatch, False)
    submit = _broken_lookup(mocker, engine)

    with caplog.at_level("ERROR"):
        response = client.post("/api/v1/jobs", json=_job_body(uuid4()))

    assert response.status_code == 201
    submit.assert_called_once()
    assert "Could not look for the upload's original (StorageError)" in caplog.text


def _broken_lookup(mocker, engine):
    class Broken(audio_storage.LocalAudioStorage):
        async def find_source(self, user_id, upload_id):
            raise audio_storage.StorageError("storage down")

    mocker.patch("transcribe_api.api.routes_recording.get_audio_storage", return_value=Broken())
    return mocker.patch(
        "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
        new=mocker.AsyncMock(side_effect=lambda **_kw: add_job(engine, source_blob_path=None)),
    )


async def test_the_rollback_sequence_an_upload_made_with_the_flag_on_is_prepared_after_it_is_turned_off(
    client, engine, monkeypatch, mocker, normalize, local_storage_dir
):
    """Upload with the flag on, turn it off, POST /jobs: the job waits (it is not sent to Azure with a
    WAV that does not exist), then the worker, which does not read the flag, prepares and submits it."""
    from transcribe_api.stt import preparation_service as prep

    monkeypatch.setenv("SPEECH_BACKEND", "mock")
    get_settings.cache_clear()
    mocker.patch.object(prep, "get_engine", new=lambda: engine)
    azure = mocker.spy(submission, "submit_batch_job")  # the mock Speech backend answers it
    _flag(monkeypatch, True)
    uploaded = _upload(client, extension_seconds=3.0)
    assert uploaded.status_code == 201
    blob_name = uploaded.json()["blob_name"]
    source = source_blob_name(USER_ID, _upload_id(blob_name), ".mp3")

    _flag(monkeypatch, False)  # the rollback
    response = client.post(
        "/api/v1/jobs", json={"audio_url": uploaded.json()["audio_url"], "blob_name": blob_name, "metadata": {}}
    )

    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    with Session(engine) as session:
        (job,) = session.exec(select(SpeechBatchJob)).all()
    assert job.status == JobStatus.PENDING and job.source_blob_path == source
    assert job.batch_job_id is None
    azure.assert_not_called()
    local_storage.read(source)  # the original is still there for the worker
    with pytest.raises(FileNotFoundError):
        local_storage.size(blob_name)

    # The worker, with the flag still off.
    service = prep.PreparationService()
    assert await service.poll_once() == 1
    await service.wait_idle()

    with Session(engine) as session:
        row = session.get(SpeechBatchJob, job.id)
    assert row.status == JobStatus.SUBMITTED and row.batch_job_id
    azure.assert_called_once()
    assert local_storage.size(blob_name) > 0  # the WAV the upload promised now exists
    with pytest.raises(FileNotFoundError):
        local_storage.size(source)  # and the original is removed


def test_a_replay_with_the_same_idempotency_key_returns_the_same_pending_job(client, engine, monkeypatch, no_azure):
    _flag(monkeypatch, True)
    upload_id = uuid4()
    _store_original(upload_id)
    first = client.post("/api/v1/jobs", json=_job_body(upload_id, idempotency_key="key-1"))
    second = client.post("/api/v1/jobs", json=_job_body(upload_id, idempotency_key="key-1"))
    assert first.status_code == second.status_code == 201
    assert first.json()["job_id"] == second.json()["job_id"]
    with Session(engine) as session:
        assert len(session.exec(select(SpeechBatchJob)).all()) == 1


def test_a_storage_failure_while_looking_for_the_original_is_a_502_and_creates_no_job(
    client, engine, monkeypatch, no_azure, mocker
):
    """Not "there is none": submitting would send Azure a WAV that does not exist."""
    _flag(monkeypatch, True)
    submit = _broken_lookup(mocker, engine)
    response = client.post("/api/v1/jobs", json=_job_body(uuid4()))
    assert response.status_code == 502
    submit.assert_not_called()
    with Session(engine) as session:
        assert session.exec(select(SpeechBatchJob)).all() == []


def test_another_users_blob_is_still_refused(client, monkeypatch, no_azure):
    _flag(monkeypatch, True)
    response = client.post(
        "/api/v1/jobs", json={"audio_url": "https://x/y.wav", "blob_name": f"uploads/{uuid4()}/{uuid4()}-hearing.wav"}
    )
    assert response.status_code == 422


def test_the_azure_original_is_looked_up_by_prefix_with_the_users_and_uploads_ids(
    client, engine, monkeypatch, no_azure, mocker
):
    _flag(monkeypatch, True)
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "azure")
    get_settings.cache_clear()
    upload_id = uuid4()
    manager = mocker.AsyncMock()
    manager.find_blob_names = mocker.AsyncMock(
        return_value=[f"incoming/{USER_ID}/{upload_id}.m4a", f"incoming/{USER_ID}/{upload_id}.m4a.tmp"]
    )
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch.object(audio_storage, "AsyncAzureBlobManager", return_value=manager)

    response = client.post("/api/v1/jobs", json=_job_body(upload_id))

    assert response.status_code == 201
    manager.find_blob_names.assert_awaited_once_with(f"incoming/{USER_ID}/{upload_id}")
    with Session(engine) as session:
        (job,) = session.exec(select(SpeechBatchJob)).all()
    assert job.source_blob_path == f"incoming/{USER_ID}/{upload_id}.m4a"  # not the .tmp that merely starts the same


# DELETE ----------------------------------------------------------------------------------------


def test_deleting_a_job_that_is_waiting_removes_its_original(client, engine, monkeypatch, local_storage_dir):
    _flag(monkeypatch, True)
    upload_id = uuid4()
    source = _store_original(upload_id)
    job = add_job(engine, upload_id=upload_id, source_blob_path=source)
    assert client.delete(f"/api/v1/jobs/{job.id}").status_code == 204
    with pytest.raises(FileNotFoundError):
        local_storage.size(source)
    with Session(engine) as session:
        assert session.get(SpeechBatchJob, job.id) is None
