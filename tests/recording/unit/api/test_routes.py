"""Unit tests for API routes."""

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from transcribe_api.api.app import create_app
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.domain.models_recording import JobStatus, SpeechBatchJob, User

_TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_job(
    status: JobStatus = JobStatus.PENDING,
    user_id: uuid.UUID | None = _TEST_USER_ID,
) -> SpeechBatchJob:
    from datetime import UTC, datetime

    job = SpeechBatchJob(
        id=uuid.uuid4(),
        caller_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        user_id=user_id,
        audio_url="https://storage.example.com/audio.wav?sig=token",
        locale="en-GB",
        status=status,
        metadata_={},
        dialogue_entries=[],
    )
    job.created_datetime = datetime(2026, 1, 1, tzinfo=UTC)
    return job


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


def _make_user() -> User:
    from datetime import UTC, datetime

    return User(
        id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        azure_user_id="test-azure-user",
        email="test@example.com",
        role="Normal",
        created_datetime=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def as_current_user(client):
    from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
    from transcribe_api.domain.auth.dependencies_recording import get_current_user

    user = _make_user()
    current_user = AuthenticatedUser(db_user=user, app_roles=["Normal"])
    app = client.app
    app.dependency_overrides[get_current_user] = lambda: current_user
    yield current_user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def as_admin_user(client):
    from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
    from transcribe_api.domain.auth.dependencies_recording import get_current_user

    user = _make_user()
    current_user = AuthenticatedUser(db_user=user, app_roles=["SystemAdministrator"])
    app = client.app
    app.dependency_overrides[get_current_user] = lambda: current_user
    yield current_user
    app.dependency_overrides.pop(get_current_user, None)


class TestHealth:
    def test_returns_ok(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_no_auth_required(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200


class TestVersion:
    def test_defaults_to_unknown(self, client, monkeypatch):
        # No GIT_SHA in the environment -> the "unknown" default.
        monkeypatch.delenv("GIT_SHA", raising=False)
        from transcribe_api.runtime.settings_recording import get_settings

        get_settings.cache_clear()
        response = client.get("/api/v1/version")
        assert response.status_code == 200
        assert response.json() == {"version": "unknown"}
        get_settings.cache_clear()

    def test_returns_baked_git_sha(self, client, monkeypatch):
        monkeypatch.setenv("GIT_SHA", "abc123def456")
        from transcribe_api.runtime.settings_recording import get_settings

        get_settings.cache_clear()
        response = client.get("/api/v1/version")
        assert response.status_code == 200
        assert response.json() == {"version": "abc123def456"}
        get_settings.cache_clear()

    def test_no_auth_required(self, client):
        response = client.get("/api/v1/version")
        assert response.status_code == 200


class TestUploadAudio:
    def _mock_blob_manager(self, mocker, *, upload_ok=True, blob_url="https://x/y.wav"):
        manager = mocker.AsyncMock()
        manager.create_blob_from_bytes = mocker.AsyncMock(return_value=upload_ok)
        manager.build_blob_url = mocker.Mock(return_value=blob_url)
        manager.__aenter__ = mocker.AsyncMock(return_value=manager)
        manager.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("transcribe_api.api.routes_recording.AsyncAzureBlobManager", return_value=manager)
        return manager

    def _mock_normalize(self, mocker, wav_bytes=b"WAVDATA"):
        """Patch out the ffmpeg transcode so uploads never touch real ffmpeg."""
        return mocker.patch(
            "transcribe_api.api.routes_recording.normalize_to_wav",
            new=mocker.AsyncMock(return_value=wav_bytes),
        )

    def test_returns_201_with_audio_url(self, client, as_current_user, mocker):
        self._mock_blob_manager(mocker)
        self._mock_normalize(mocker)

        response = client.post(
            "/api/v1/uploads",
            files={"file": ("hearing.mp3", b"fake-audio-bytes", "audio/mpeg")},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["audio_url"] == "https://x/y.wav"
        # Stored object is the transcoded WAV, keeping the sanitised stem.
        assert body["blob_name"].endswith("hearing.wav")

    def test_stores_normalized_wav_bytes(self, client, as_current_user, mocker):
        manager = self._mock_blob_manager(mocker)
        self._mock_normalize(mocker, wav_bytes=b"WAVDATA")

        response = client.post(
            "/api/v1/uploads",
            files={"file": ("hearing.mp3", b"fake-audio-bytes", "audio/mpeg")},
        )
        assert response.status_code == 201
        stored_bytes = manager.create_blob_from_bytes.call_args.args[0]
        assert stored_bytes == b"WAVDATA"

    def test_returns_422_when_audio_cannot_be_decoded(self, client, as_current_user, mocker):
        from transcribe_api.stt.preprocessing import AudioDecodeError

        manager = self._mock_blob_manager(mocker)
        mocker.patch(
            "transcribe_api.api.routes_recording.normalize_to_wav",
            new=mocker.AsyncMock(side_effect=AudioDecodeError("bad")),
        )

        response = client.post(
            "/api/v1/uploads",
            files={"file": ("hearing.mp3", b"not-real-audio", "audio/mpeg")},
        )
        assert response.status_code == 422
        # No blob is stored for an undecodable upload — no doomed job is created.
        manager.create_blob_from_bytes.assert_not_called()

    def test_rejects_unsupported_extension(self, client, as_current_user):
        response = client.post(
            "/api/v1/uploads",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 422

    def test_returns_502_when_storage_upload_fails(self, client, as_current_user, mocker):
        self._mock_blob_manager(mocker, upload_ok=False)
        self._mock_normalize(mocker)

        response = client.post(
            "/api/v1/uploads",
            files={"file": ("hearing.mp3", b"fake-audio-bytes", "audio/mpeg")},
        )
        assert response.status_code == 502

    def test_requires_auth(self, client):
        response = client.post(
            "/api/v1/uploads",
            files={"file": ("hearing.mp3", b"fake-audio-bytes", "audio/mpeg")},
        )
        assert response.status_code in (401, 422)


class TestUploadAudioLocalBackend:
    @pytest.fixture(autouse=True)
    def local_backend(self, tmp_path, monkeypatch):
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "https://abc123.ngrok-free.app")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_stores_locally_and_returns_tunnel_url(self, client, as_current_user, mocker):
        mocker.patch(
            "transcribe_api.api.routes_recording.normalize_to_wav",
            new=mocker.AsyncMock(return_value=b"WAVDATA"),
        )
        response = client.post(
            "/api/v1/uploads",
            files={"file": ("hearing.mp3", b"fake-audio-bytes", "audio/mpeg")},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["audio_url"].startswith("https://abc123.ngrok-free.app/api/v1/local-audio/")

        get_response = client.get(body["audio_url"].replace("https://abc123.ngrok-free.app", ""))
        assert get_response.status_code == 200
        # The stored/served bytes are the transcoded WAV, not the raw upload.
        assert get_response.content == b"WAVDATA"

    def test_never_touches_azure_blob_manager(self, client, as_current_user, mocker):
        blob_manager_cls = mocker.patch("transcribe_api.api.routes_recording.AsyncAzureBlobManager")
        mocker.patch(
            "transcribe_api.api.routes_recording.normalize_to_wav",
            new=mocker.AsyncMock(return_value=b"WAVDATA"),
        )

        client.post(
            "/api/v1/uploads",
            files={"file": ("hearing.mp3", b"fake-audio-bytes", "audio/mpeg")},
        )

        blob_manager_cls.assert_not_called()


class TestLocalAudio:
    def test_404_when_backend_is_not_local(self, client):
        response = client.get("/api/v1/local-audio/uploads/x/hearing.wav")
        assert response.status_code == 404

    def test_404_for_path_traversal(self, tmp_path, monkeypatch, client):
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        get_settings.cache_clear()

        response = client.get("/api/v1/local-audio/../../etc/passwd")

        get_settings.cache_clear()
        assert response.status_code == 404


class TestGetJobAudio:
    def test_returns_404_for_unknown_job(self, client, as_current_user, mocker):
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=None)
        response = client.get(f"/api/v1/jobs/{uuid.uuid4()}/audio")
        assert response.status_code == 404

    def test_returns_404_for_other_callers_job(self, client, as_current_user, mocker):
        job = _make_job()
        job.user_id = uuid.uuid4()
        job.audio_blob_path = "uploads/x/hearing.wav"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        response = client.get(f"/api/v1/jobs/{job.id}/audio")
        assert response.status_code == 404

    def test_returns_404_when_job_has_no_blob_path(self, client, as_current_user, mocker):
        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = None
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        response = client.get(f"/api/v1/jobs/{job.id}/audio")
        assert response.status_code == 404

    def test_streams_full_content_from_local_backend(
        self, client, as_current_user, mocker, tmp_path, monkeypatch
    ):
        from transcribe_api.stt import local_storage
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        get_settings.cache_clear()

        blob_name = "uploads/x/hearing.wav"
        local_storage.save(b"fake-audio-bytes", blob_name)

        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = blob_name
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}/audio")
        get_settings.cache_clear()

        assert response.status_code == 200
        assert response.content == b"fake-audio-bytes"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-length"] == "16"

    def test_streams_partial_range_from_local_backend(
        self, client, as_current_user, mocker, tmp_path, monkeypatch
    ):
        from transcribe_api.stt import local_storage
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        get_settings.cache_clear()

        blob_name = "uploads/x/hearing.wav"
        local_storage.save(b"0123456789", blob_name)

        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = blob_name
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}/audio", headers={"Range": "bytes=2-4"})
        get_settings.cache_clear()

        assert response.status_code == 206
        assert response.content == b"234"
        assert response.headers["content-range"] == "bytes 2-4/10"
        assert response.headers["content-length"] == "3"

    def test_returns_404_when_local_file_missing(
        self, client, as_current_user, mocker, tmp_path, monkeypatch
    ):
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        get_settings.cache_clear()

        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = "uploads/x/missing.wav"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}/audio")
        get_settings.cache_clear()

        assert response.status_code == 404

    def test_supports_suffix_range_for_the_last_n_bytes(
        self, client, as_current_user, mocker, tmp_path, monkeypatch
    ):
        from transcribe_api.stt import local_storage
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        get_settings.cache_clear()

        blob_name = "uploads/x/hearing.wav"
        local_storage.save(b"0123456789", blob_name)

        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = blob_name
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        # "bytes=-3" means "the last 3 bytes", not "bytes 0 to 3".
        response = client.get(f"/api/v1/jobs/{job.id}/audio", headers={"Range": "bytes=-3"})
        get_settings.cache_clear()

        assert response.status_code == 206
        assert response.content == b"789"
        assert response.headers["content-range"] == "bytes 7-9/10"

    def test_returns_416_for_a_malformed_range_header(
        self, client, as_current_user, mocker, tmp_path, monkeypatch
    ):
        from transcribe_api.stt import local_storage
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        get_settings.cache_clear()

        blob_name = "uploads/x/hearing.wav"
        local_storage.save(b"0123456789", blob_name)

        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = blob_name
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        # Multi-range requests aren't supported; this must not be silently
        # misinterpreted as the first sub-range.
        response = client.get(f"/api/v1/jobs/{job.id}/audio", headers={"Range": "bytes=0-1,2-3"})
        get_settings.cache_clear()

        assert response.status_code == 416
        assert response.headers["content-range"] == "bytes */10"

    def test_returns_416_for_an_out_of_bounds_range(
        self, client, as_current_user, mocker, tmp_path, monkeypatch
    ):
        from transcribe_api.stt import local_storage
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        get_settings.cache_clear()

        blob_name = "uploads/x/hearing.wav"
        local_storage.save(b"0123456789", blob_name)

        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = blob_name
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}/audio", headers={"Range": "bytes=20-30"})
        get_settings.cache_clear()

        assert response.status_code == 416
        assert response.headers["content-range"] == "bytes */10"

    def test_returns_200_with_empty_body_for_a_zero_byte_file(
        self, client, as_current_user, mocker, tmp_path, monkeypatch
    ):
        from transcribe_api.stt import local_storage
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        get_settings.cache_clear()

        blob_name = "uploads/x/empty.wav"
        local_storage.save(b"", blob_name)

        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = blob_name
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}/audio")
        get_settings.cache_clear()

        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["content-length"] == "0"

    def test_returns_416_for_a_range_request_against_a_zero_byte_file(
        self, client, as_current_user, mocker, tmp_path, monkeypatch
    ):
        from transcribe_api.stt import local_storage
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        get_settings.cache_clear()

        blob_name = "uploads/x/empty.wav"
        local_storage.save(b"", blob_name)

        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = blob_name
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}/audio", headers={"Range": "bytes=0-9"})
        get_settings.cache_clear()

        assert response.status_code == 416
        assert response.headers["content-range"] == "bytes */0"

    def test_streams_partial_range_from_azure_backend(self, client, as_current_user, mocker):
        async def achunks(chunks):
            for c in chunks:
                yield c

        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = "uploads/x/hearing.wav"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        manager = mocker.AsyncMock()
        manager.get_blob_size = mocker.AsyncMock(return_value=100)
        manager.stream_blob_range = mocker.MagicMock(return_value=achunks([b"partial-", b"bytes"]))
        manager.close = mocker.AsyncMock()
        manager.__aenter__ = mocker.AsyncMock(return_value=manager)
        manager.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("transcribe_api.api.routes_recording.AsyncAzureBlobManager", return_value=manager)

        response = client.get(f"/api/v1/jobs/{job.id}/audio", headers={"Range": "bytes=10-30"})

        assert response.status_code == 206
        assert response.content == b"partial-bytes"
        assert response.headers["content-range"] == "bytes 10-30/100"
        manager.stream_blob_range.assert_called_once_with("uploads/x/hearing.wav", 10, 21)
        manager.close.assert_awaited_once()

    def test_returns_404_when_azure_blob_missing(self, client, as_current_user, mocker):
        job = _make_job()
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_blob_path = "uploads/x/missing.wav"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        manager = mocker.AsyncMock()
        manager.get_blob_size = mocker.AsyncMock(return_value=None)
        manager.__aenter__ = mocker.AsyncMock(return_value=manager)
        manager.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("transcribe_api.api.routes_recording.AsyncAzureBlobManager", return_value=manager)

        response = client.get(f"/api/v1/jobs/{job.id}/audio")

        assert response.status_code == 404


class TestSubmitJob:
    def test_returns_201_on_success(self, client, as_current_user, mocker):
        job = _make_job()
        mocker.patch(
            "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
            return_value=job,
        )
        mocker.patch(
            "transcribe_api.api.routes_recording.get_job_by_idempotency_key_for_user", return_value=None
        )

        response = client.post(
            "/api/v1/jobs",
            json={
                "audio_url": "https://storage.example.com/audio.wav?sig=token",
                "metadata": {"transcription_id": "t-1", "user_id": "u-1"},
            },
        )
        assert response.status_code == 201
        assert "job_id" in response.json()

    def test_returns_existing_job_on_idempotency_hit(self, client, as_current_user, mocker):
        existing = _make_job(status=JobStatus.SUCCEEDED)
        mocker.patch(
            "transcribe_api.api.routes_recording.get_job_by_idempotency_key_for_user",
            return_value=existing,
        )

        response = client.post(
            "/api/v1/jobs",
            json={
                "audio_url": "https://storage.example.com/audio.wav?sig=token",
                "idempotency_key": "my-key",
                "metadata": {"transcription_id": "t-1", "user_id": "u-1"},
            },
        )
        assert response.status_code == 201
        assert response.json()["status"] == "succeeded"

    def test_requires_auth(self, client):
        response = client.post(
            "/api/v1/jobs",
            json={"audio_url": "https://storage.example.com/audio.wav"},
        )
        assert response.status_code in (401, 422)

    def test_accepts_blob_name_under_callers_own_prefix(self, client, as_current_user, mocker):
        job = _make_job()
        submit_mock = mocker.patch(
            "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
            return_value=job,
        )
        mocker.patch(
            "transcribe_api.api.routes_recording.get_job_by_idempotency_key_for_user", return_value=None
        )

        # as_current_user's id is 00000000-0000-0000-0000-000000000002 (see _make_user).
        own_blob_name = "uploads/00000000-0000-0000-0000-000000000002/audio.wav"
        response = client.post(
            "/api/v1/jobs",
            json={
                "audio_url": "https://storage.example.com/audio.wav?sig=token",
                "blob_name": own_blob_name,
            },
        )
        assert response.status_code == 201
        assert submit_mock.call_args.kwargs["audio_blob_path"] == own_blob_name

    def test_rejects_blob_name_belonging_to_another_caller(self, client, as_current_user, mocker):
        # blob_name is later trusted by GET /jobs/{id}/audio to read straight
        # from storage — without this check a caller could read another
        # caller's audio by guessing/observing their blob path.
        mocker.patch(
            "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
            return_value=_make_job(),
        )
        mocker.patch(
            "transcribe_api.api.routes_recording.get_job_by_idempotency_key_for_user", return_value=None
        )

        response = client.post(
            "/api/v1/jobs",
            json={
                "audio_url": "https://storage.example.com/audio.wav?sig=token",
                "blob_name": "uploads/11111111-1111-1111-1111-111111111111/audio.wav",
            },
        )
        assert response.status_code == 422

    def test_passes_audio_duration_seconds_through_to_submission(
        self, client, as_current_user, mocker
    ):
        job = _make_job()
        submit_mock = mocker.patch(
            "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
            return_value=job,
        )
        mocker.patch(
            "transcribe_api.api.routes_recording.get_job_by_idempotency_key_for_user", return_value=None
        )

        response = client.post(
            "/api/v1/jobs",
            json={
                "audio_url": "https://storage.example.com/audio.wav?sig=token",
                "audio_duration_seconds": 9360.0,
            },
        )
        assert response.status_code == 201
        assert submit_mock.call_args.kwargs["audio_duration_seconds"] == 9360.0

    def test_audio_duration_seconds_is_optional(self, client, as_current_user, mocker):
        job = _make_job()
        submit_mock = mocker.patch(
            "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
            return_value=job,
        )
        mocker.patch(
            "transcribe_api.api.routes_recording.get_job_by_idempotency_key_for_user", return_value=None
        )

        response = client.post(
            "/api/v1/jobs",
            json={"audio_url": "https://storage.example.com/audio.wav?sig=token"},
        )
        assert response.status_code == 201
        assert submit_mock.call_args.kwargs["audio_duration_seconds"] is None

    def test_rejects_negative_audio_duration_seconds(self, client, as_current_user, mocker):
        mocker.patch(
            "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
            return_value=_make_job(),
        )
        mocker.patch(
            "transcribe_api.api.routes_recording.get_job_by_idempotency_key_for_user", return_value=None
        )

        response = client.post(
            "/api/v1/jobs",
            json={
                "audio_url": "https://storage.example.com/audio.wav?sig=token",
                "audio_duration_seconds": -1.0,
            },
        )
        assert response.status_code == 422

    def test_callback_url_not_accepted(self, client, as_current_user, mocker):
        # callback_url is removed from SubmitJobRequest so it is not part of the
        # OpenAPI schema. Sending it in the body is harmless (Pydantic drops unknown
        # fields) but the submission should succeed — the field is simply not wired up.
        mocker.patch(
            "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
            return_value=_make_job(),
        )
        mocker.patch(
            "transcribe_api.api.routes_recording.get_job_by_idempotency_key_for_user",
            return_value=None,
        )
        response = client.post(
            "/api/v1/jobs",
            json={
                "audio_url": "https://storage.example.com/audio.wav?sig=token",
                "callback_url": "https://example.com/webhook",
            },
        )
        assert response.status_code == 201

    @pytest.mark.parametrize("bad_value", ["NaN", "Infinity", "-Infinity"])
    def test_rejects_non_finite_audio_duration_seconds(
        self, client, as_current_user, mocker, bad_value
    ):
        # Python's json module accepts these tokens and Pydantic coerces them to
        # float, so they must be rejected explicitly — they can't be serialised
        # back in a JSON response.
        mocker.patch(
            "transcribe_api.api.routes_recording.submit_and_queue_batch_job",
            return_value=_make_job(),
        )
        mocker.patch(
            "transcribe_api.api.routes_recording.get_job_by_idempotency_key_for_user", return_value=None
        )

        response = client.post(
            "/api/v1/jobs",
            # Sent as a raw JSON body so the non-finite literals reach Pydantic
            # exactly as a permissive client would send them.
            content=(
                '{"audio_url": "https://storage.example.com/audio.wav?sig=token", '
                f'"audio_duration_seconds": {bad_value}}}'
            ),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422


class TestGetJob:
    def test_returns_job(self, client, as_current_user, mocker):
        job = _make_job(user_id=as_current_user.db_user.id)
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 200

    def test_caller_name_absent_from_response(self, client, as_current_user, mocker):
        # caller_name was removed in DIAAT-20 when API-key auth was replaced by
        # JWT. The field no longer exists on JobResponse.
        job = _make_job()
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 200
        assert "caller_name" not in response.json()

    def test_returns_404_for_unknown_job(self, client, as_current_user, mocker):
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=None)
        response = client.get(f"/api/v1/jobs/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_returns_404_for_other_callers_job(self, client, as_current_user, mocker):
        job = _make_job()
        job.user_id = uuid.uuid4()  # different user
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 404

    def test_legacy_job_returns_404_for_normal_user(self, client, as_current_user, mocker):
        # Pre-migration jobs (user_id=None) may contain sensitive hearing content;
        # both reads and writes are restricted to SystemAdministrator.
        job = _make_job(user_id=None)
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 404

    def test_legacy_job_readable_by_system_administrator(self, client, as_admin_user, mocker):
        job = _make_job(user_id=None)
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 200

    def test_includes_accuracy_and_needs_review_for_succeeded_job(
        self, client, as_current_user, mocker
    ):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "hello there",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.5,
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        body = response.json()

        assert body["accuracy"]["confidence_score"] == 50.0
        assert body["accuracy"]["has_corrections"] is False
        assert body["accuracy"]["word_error_rate"] is None
        assert len(body["needs_review"]) == 1
        assert body["needs_review"][0]["speaker"] == "0"

    def test_omits_accuracy_for_non_succeeded_job(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUBMITTED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        body = response.json()

        assert body["accuracy"] is None
        assert body["needs_review"] is None

    def test_includes_audio_duration_seconds_when_known(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.RUNNING)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_duration_seconds = 9360.0
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.json()["audio_duration_seconds"] == 9360.0

    def test_audio_duration_seconds_is_none_when_unknown(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.RUNNING)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.json()["audio_duration_seconds"] is None

    def test_round_trips_nbest_alternatives(self, client, as_current_user, mocker):
        # DIAAT-232: the full nBest array persisted per phrase (not just
        # Azure's top choice, already covered by text/confidence/words)
        # must survive storage and come back out through the API untouched.
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "Hello world.",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.56,
                "words": _words_payload("hello", "world"),
                "alternatives": [
                    {
                        "start_word_index": 0,
                        "end_word_index": 1,
                        "candidates": [
                            {"text": "Hello world.", "confidence": 0.56, "lexical": "hello world"},
                            {"text": "helloworld", "confidence": 0.18, "lexical": "helloworld"},
                            {
                                "text": "hello worlds",
                                "confidence": 0.5,
                                "lexical": "hello worlds",
                            },
                        ],
                    }
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        entry = response.json()["dialogue_entries"][0]

        assert entry["alternatives"] == [
            {
                "start_word_index": 0,
                "end_word_index": 1,
                "candidates": [
                    {"text": "Hello world.", "confidence": 0.56, "lexical": "hello world"},
                    {"text": "helloworld", "confidence": 0.18, "lexical": "helloworld"},
                    {"text": "hello worlds", "confidence": 0.5, "lexical": "hello worlds"},
                ],
            }
        ]

    def test_alternatives_is_none_when_not_present(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        entry = response.json()["dialogue_entries"][0]

        assert entry["alternatives"] is None

    # DIAAT-235: LOW_CONFIDENCE_THRESHOLD lets ops override the review
    # threshold per environment without a code change; previously declared
    # in Settings but never actually wired into the accuracy computation.
    def test_low_confidence_threshold_setting_overrides_the_default(
        self, client, as_current_user, mocker, monkeypatch
    ):
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("LOW_CONFIDENCE_THRESHOLD", "0.9")
        get_settings.cache_clear()
        try:
            job = _make_job(status=JobStatus.SUCCEEDED)
            job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
            job.dialogue_entries = [
                {
                    "speaker": "0",
                    "text": "hello there",
                    "start_time": 0.0,
                    "end_time": 1.0,
                    # Above the 0.65 code default but below the 0.9 override —
                    # only flagged once the setting is actually applied.
                    "confidence": 0.75,
                }
            ]
            mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

            response = client.get(f"/api/v1/jobs/{job.id}")
            body = response.json()

            assert body["accuracy"]["confidence_threshold"] == 90.0
            assert len(body["needs_review"]) == 1
        finally:
            get_settings.cache_clear()

    def test_includes_run_metadata_for_completed_job(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.audio_duration_seconds = 754.2
        job.transcription_duration_seconds = 41.8
        job.model_identifier = "https://eastus.example.com/models/base/xyz"
        job.model_display_name = "20240614 Base — en-GB"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        body = response.json()

        assert body["audio_duration_seconds"] == 754.2
        assert body["transcription_duration_seconds"] == 41.8
        assert body["model_identifier"] == "https://eastus.example.com/models/base/xyz"
        assert body["model_display_name"] == "20240614 Base — en-GB"

    def test_run_metadata_defaults_to_null_before_completion(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUBMITTED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.get(f"/api/v1/jobs/{job.id}")
        body = response.json()

        assert body["audio_duration_seconds"] is None
        assert body["transcription_duration_seconds"] is None
        assert body["model_identifier"] is None
        assert body["model_display_name"] is None


class TestUploadBaselineTranscript:
    def _patch_session(self, client, mocker):
        from transcribe_api.runtime.db_recording import get_session

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        return mock_session

    def test_returns_404_for_unknown_job(self, client, as_current_user, mocker):
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=None)
        response = client.post(
            f"/api/v1/jobs/{uuid.uuid4()}/baseline",
            files={"file": ("baseline.txt", b"hello world", "text/plain")},
        )
        assert response.status_code == 404

    def test_returns_404_for_other_callers_job(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.user_id = uuid.uuid4()
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(
            f"/api/v1/jobs/{job.id}/baseline",
            files={"file": ("baseline.txt", b"hello world", "text/plain")},
        )
        assert response.status_code == 404

    def test_legacy_job_returns_404_for_normal_user(self, client, as_current_user, mocker):
        # Pre-migration jobs (user_id=None) are restricted to SystemAdministrator
        # for both reads and writes.
        job = _make_job(status=JobStatus.SUCCEEDED, user_id=None)
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(
            f"/api/v1/jobs/{job.id}/baseline",
            files={"file": ("baseline.txt", b"hello world", "text/plain")},
        )
        assert response.status_code == 404

    def test_legacy_job_accessible_to_system_administrator(self, client, as_admin_user, mocker):
        # SystemAdministrators may mutate legacy (user_id=None) jobs.
        from transcribe_api.runtime.db_recording import get_session

        self._patch_session(client, mocker)
        job = _make_job(status=JobStatus.SUCCEEDED, user_id=None)
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        try:
            response = client.post(
                f"/api/v1/jobs/{job.id}/baseline",
                files={"file": ("baseline.txt", b"hello world", "text/plain")},
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200

    def test_returns_422_when_job_not_succeeded(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUBMITTED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(
            f"/api/v1/jobs/{job.id}/baseline",
            files={"file": ("baseline.txt", b"hello world", "text/plain")},
        )
        assert response.status_code == 422

    def test_rejects_unsupported_extension(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(
            f"/api/v1/jobs/{job.id}/baseline",
            files={"file": ("baseline.pdf", b"hello world", "application/pdf")},
        )
        assert response.status_code == 422

    def test_rejects_missing_extension(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(
            f"/api/v1/jobs/{job.id}/baseline",
            files={"file": ("baseline", b"hello world", "text/plain")},
        )
        assert response.status_code == 422

    def test_rejects_empty_file(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(
            f"/api/v1/jobs/{job.id}/baseline",
            files={"file": ("baseline.txt", b"   ", "text/plain")},
        )
        assert response.status_code == 422

    def test_rejects_non_utf8_content(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(
            f"/api/v1/jobs/{job.id}/baseline",
            files={"file": ("baseline.txt", b"\xff\xfe\x00\x01", "text/plain")},
        )
        assert response.status_code == 422

    def test_stores_baseline_and_returns_wer(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.9,
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.post(
                f"/api/v1/jobs/{job.id}/baseline",
                files={"file": ("baseline.txt", b"the slow brown fox", "text/plain")},
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        body = response.json()
        assert body["accuracy"]["has_baseline"] is True
        assert body["accuracy"]["baseline_word_error_rate"] == 25.0
        assert job.baseline_transcript == "the slow brown fox"
        mock_session.commit.assert_called_once()

    def test_strips_utf8_bom_from_baseline(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.9,
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        self._patch_session(client, mocker)

        # A Windows-exported .txt often starts with a UTF-8 BOM; it must be
        # stripped so it isn't glued onto the first word (which would make an
        # otherwise-identical baseline report a non-zero WER).
        try:
            response = client.post(
                f"/api/v1/jobs/{job.id}/baseline",
                files={
                    "file": (
                        "baseline.txt",
                        b"\xef\xbb\xbf" + b"the quick brown fox",  # UTF-8 BOM + text
                        "text/plain",
                    )
                },
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        assert job.baseline_transcript == "the quick brown fox"
        assert response.json()["accuracy"]["baseline_word_error_rate"] == 0.0

    def test_requires_auth(self, client):
        response = client.post(
            f"/api/v1/jobs/{uuid.uuid4()}/baseline",
            files={"file": ("baseline.txt", b"hello world", "text/plain")},
        )
        assert response.status_code in (401, 422)


class TestCorrectSegment:
    def _patch_session(self, client, mocker):
        from transcribe_api.runtime.db_recording import get_session

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        return mock_session

    def test_returns_404_for_unknown_job(self, client, as_current_user, mocker):
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=None)
        response = client.patch(
            f"/api/v1/jobs/{uuid.uuid4()}/segments/0", json={"corrected_text": "fixed"}
        )
        assert response.status_code == 404

    def test_returns_404_for_other_callers_job(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.user_id = uuid.uuid4()
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/0", json={"corrected_text": "fixed"}
        )
        assert response.status_code == 404

    def test_returns_422_when_job_not_succeeded(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUBMITTED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/0", json={"corrected_text": "fixed"}
        )
        assert response.status_code == 422

    def test_returns_404_for_out_of_range_index(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/5", json={"corrected_text": "fixed"}
        )
        assert response.status_code == 404

    def test_stores_correction_and_returns_updated_job(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.9,
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.patch(
                f"/api/v1/jobs/{job.id}/segments/0",
                json={"corrected_text": "the slow brown fox"},
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        body = response.json()
        assert body["dialogue_entries"][0]["corrected_text"] == "the slow brown fox"
        assert body["dialogue_entries"][0]["text"] == "the quick brown fox"
        assert body["accuracy"]["has_corrections"] is True
        mock_session.commit.assert_called_once()

    def test_does_not_write_dataset_entry_when_flag_disabled(
        self, client, as_current_user, mocker, monkeypatch
    ):
        """DIAAT-231: default-off flag means no row in correction_dataset_entry."""
        from transcribe_api.runtime.settings_recording import get_settings
        from transcribe_api.runtime.db_recording import get_session
        from transcribe_api.domain.models_recording import CorrectionDatasetEntry

        monkeypatch.setenv("CORRECTIONS_DATASET_EXPORT_ENABLED", "false")
        get_settings.cache_clear()
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.9,
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            client.patch(
                f"/api/v1/jobs/{job.id}/segments/0",
                json={"corrected_text": "the slow brown fox"},
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        dataset_rows = [
            call.args[0]
            for call in mock_session.add.call_args_list
            if isinstance(call.args[0], CorrectionDatasetEntry)
        ]
        assert dataset_rows == []

    def test_writes_dataset_entry_when_flag_enabled(
        self, client, as_current_user, mocker, monkeypatch
    ):
        """DIAAT-231: enabling the flag stages a row capturing the correction."""
        from transcribe_api.runtime.settings_recording import get_settings
        from transcribe_api.runtime.db_recording import get_session
        from transcribe_api.domain.models_recording import CorrectionDatasetEntry

        monkeypatch.setenv("CORRECTIONS_DATASET_EXPORT_ENABLED", "true")
        get_settings.cache_clear()
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.9,
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.patch(
                f"/api/v1/jobs/{job.id}/segments/0",
                json={"corrected_text": "the slow brown fox"},
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        dataset_rows = [
            call.args[0]
            for call in mock_session.add.call_args_list
            if isinstance(call.args[0], CorrectionDatasetEntry)
        ]
        assert len(dataset_rows) == 1
        row = dataset_rows[0]
        assert row.job_id == job.id
        assert row.caller_id == job.caller_id
        assert row.segment_index == 0
        assert row.correction_kind == "segment"
        assert row.original_text == "the quick brown fox"
        assert row.corrected_text == "the slow brown fox"
        assert row.confidence == 0.9
        assert row.speaker == "0"
        # Staged in the same commit as the job update — atomic with it.
        mock_session.commit.assert_called_once()

    def test_rejects_empty_correction(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(f"/api/v1/jobs/{job.id}/segments/0", json={"corrected_text": ""})
        assert response.status_code == 422

    def test_rejects_whitespace_only_correction(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(f"/api/v1/jobs/{job.id}/segments/0", json={"corrected_text": "   "})
        assert response.status_code == 422


def _words_payload(*texts: str) -> list[dict]:
    return [
        {"text": t, "start_time": float(i), "end_time": float(i) + 1, "confidence": 0.9}
        for i, t in enumerate(texts)
    ]


class TestCorrectWordRange:
    def _patch_session(self, client, mocker):
        from transcribe_api.runtime.db_recording import get_session

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        return mock_session

    def test_returns_404_for_unknown_job(self, client, as_current_user, mocker):
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=None)
        response = client.patch(
            f"/api/v1/jobs/{uuid.uuid4()}/segments/0/words",
            json={"start_word_index": 0, "end_word_index": 0, "corrected_text": "fixed"},
        )
        assert response.status_code == 404

    def test_returns_404_for_other_callers_job(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.user_id = uuid.uuid4()
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0,
                "end_time": 1,
                "words": _words_payload("the", "quick", "brown", "fox"),
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/0/words",
            json={"start_word_index": 0, "end_word_index": 0, "corrected_text": "fixed"},
        )
        assert response.status_code == 404

    def test_returns_422_when_job_not_succeeded(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUBMITTED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/0/words",
            json={"start_word_index": 0, "end_word_index": 0, "corrected_text": "fixed"},
        )
        assert response.status_code == 422

    def test_returns_404_for_out_of_range_index(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/5/words",
            json={"start_word_index": 0, "end_word_index": 0, "corrected_text": "fixed"},
        )
        assert response.status_code == 404

    def test_returns_422_when_segment_has_no_words(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {"speaker": "0", "text": "the quick brown fox", "start_time": 0, "end_time": 1}
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/0/words",
            json={"start_word_index": 0, "end_word_index": 0, "corrected_text": "fixed"},
        )
        assert response.status_code == 422

    def test_returns_422_for_invalid_range(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0,
                "end_time": 1,
                "words": _words_payload("the", "quick", "brown", "fox"),
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/0/words",
            json={"start_word_index": 2, "end_word_index": 1, "corrected_text": "fixed"},
        )
        assert response.status_code == 422

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/0/words",
            json={"start_word_index": 0, "end_word_index": 10, "corrected_text": "fixed"},
        )
        assert response.status_code == 422

    def test_rejects_whitespace_only_correction(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0,
                "end_time": 1,
                "words": _words_payload("the", "quick", "brown", "fox"),
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/0/words",
            json={"start_word_index": 0, "end_word_index": 0, "corrected_text": "   "},
        )
        assert response.status_code == 422

    def test_returns_422_when_whole_segment_already_corrected(
        self, client, as_current_user, mocker
    ):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0,
                "end_time": 1,
                "corrected_text": "a different sentence entirely",
                "words": _words_payload("the", "quick", "brown", "fox"),
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/0/words",
            json={"start_word_index": 0, "end_word_index": 0, "corrected_text": "fixed"},
        )
        assert response.status_code == 422

    def test_stores_word_range_correction_and_preserves_other_words(
        self, client, as_current_user, mocker
    ):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.9,
                "words": _words_payload("the", "quick", "brown", "fox"),
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.patch(
                f"/api/v1/jobs/{job.id}/segments/0/words",
                json={"start_word_index": 1, "end_word_index": 1, "corrected_text": "slow"},
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        body = response.json()
        entry = body["dialogue_entries"][0]
        assert entry["corrected_text"] is None
        assert entry["word_corrections"] == [
            {"start_word_index": 1, "end_word_index": 1, "text": "slow"}
        ]
        assert entry["words"] is not None  # untouched per-word data still present
        assert len(entry["correction_history"]) == 1
        assert entry["correction_history"][0]["kind"] == "word_range"
        assert entry["correction_history"][0]["previous_text"] == "the quick brown fox"
        assert entry["correction_history"][0]["new_text"] == "the slow brown fox"
        # The concise phrase-only diff — what a clerk actually wants to see
        # in a history list, as opposed to replaying the whole segment.
        assert entry["correction_history"][0]["previous_phrase"] == "quick"
        assert entry["correction_history"][0]["new_phrase"] == "slow"
        mock_session.commit.assert_called_once()

    def test_does_not_write_dataset_entry_when_flag_disabled(
        self, client, as_current_user, mocker, monkeypatch
    ):
        """DIAAT-231: default-off flag means no row in correction_dataset_entry."""
        from transcribe_api.runtime.settings_recording import get_settings
        from transcribe_api.runtime.db_recording import get_session
        from transcribe_api.domain.models_recording import CorrectionDatasetEntry

        monkeypatch.setenv("CORRECTIONS_DATASET_EXPORT_ENABLED", "false")
        get_settings.cache_clear()
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.9,
                "words": _words_payload("the", "quick", "brown", "fox"),
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            client.patch(
                f"/api/v1/jobs/{job.id}/segments/0/words",
                json={"start_word_index": 1, "end_word_index": 1, "corrected_text": "slow"},
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        dataset_rows = [
            call.args[0]
            for call in mock_session.add.call_args_list
            if isinstance(call.args[0], CorrectionDatasetEntry)
        ]
        assert dataset_rows == []

    def test_writes_dataset_entry_when_flag_enabled(
        self, client, as_current_user, mocker, monkeypatch
    ):
        """DIAAT-231: enabling the flag stages a row with the original lexical

        phrase (from entry.words, not any prior correction) paired with the
        clerk's corrected phrase and the per-word confidence for that range.
        """
        from transcribe_api.runtime.settings_recording import get_settings
        from transcribe_api.runtime.db_recording import get_session
        from transcribe_api.domain.models_recording import CorrectionDatasetEntry

        monkeypatch.setenv("CORRECTIONS_DATASET_EXPORT_ENABLED", "true")
        get_settings.cache_clear()
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.9,
                "words": _words_payload("the", "quick", "brown", "fox"),
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.patch(
                f"/api/v1/jobs/{job.id}/segments/0/words",
                json={"start_word_index": 1, "end_word_index": 1, "corrected_text": "slow"},
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        dataset_rows = [
            call.args[0]
            for call in mock_session.add.call_args_list
            if isinstance(call.args[0], CorrectionDatasetEntry)
        ]
        assert len(dataset_rows) == 1
        row = dataset_rows[0]
        assert row.correction_kind == "word_range"
        assert row.original_text == "quick"
        assert row.corrected_text == "slow"
        assert row.start_word_index == 1
        assert row.end_word_index == 1
        assert row.confidence == 0.9

    def test_new_range_supersedes_overlapping_existing_correction(
        self, client, as_current_user, mocker
    ):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "words": _words_payload("the", "quick", "brown", "fox"),
                "word_corrections": [
                    {"start_word_index": 1, "end_word_index": 2, "text": "very slow"}
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.patch(
                f"/api/v1/jobs/{job.id}/segments/0/words",
                json={"start_word_index": 2, "end_word_index": 2, "corrected_text": "grey"},
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        entry = response.json()["dialogue_entries"][0]
        assert entry["word_corrections"] == [
            {"start_word_index": 2, "end_word_index": 2, "text": "grey"}
        ]
        mock_session.commit.assert_called_once()

    def test_re_editing_the_same_range_logs_the_prior_correction_as_previous_phrase(
        self, client, as_current_user, mocker
    ):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "words": _words_payload("the", "quick", "brown", "fox"),
                "word_corrections": [{"start_word_index": 1, "end_word_index": 1, "text": "slow"}],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.patch(
                f"/api/v1/jobs/{job.id}/segments/0/words",
                json={"start_word_index": 1, "end_word_index": 1, "corrected_text": "sluggish"},
            )
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        history = response.json()["dialogue_entries"][0]["correction_history"]
        # The previous phrase should reflect the existing correction
        # ("slow"), not the original word ("quick").
        assert history[0]["previous_phrase"] == "slow"
        assert history[0]["new_phrase"] == "sluggish"
        mock_session.commit.assert_called_once()


class TestAcceptSegment:
    def _patch_session(self, client, mocker):
        from transcribe_api.runtime.db_recording import get_session

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        return mock_session

    def test_returns_404_for_unknown_job(self, client, as_current_user, mocker):
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=None)
        response = client.post(f"/api/v1/jobs/{uuid.uuid4()}/segments/0/accept")
        assert response.status_code == 404

    def test_returns_404_for_other_callers_job(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.user_id = uuid.uuid4()
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(f"/api/v1/jobs/{job.id}/segments/0/accept")
        assert response.status_code == 404

    def test_returns_422_when_job_not_succeeded(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUBMITTED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(f"/api/v1/jobs/{job.id}/segments/0/accept")
        assert response.status_code == 422

    def test_returns_404_for_out_of_range_index(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(f"/api/v1/jobs/{job.id}/segments/5/accept")
        assert response.status_code == 404

    def test_marks_segment_accepted_without_changing_text(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.4,
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/accept")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        body = response.json()
        entry = body["dialogue_entries"][0]
        # Underlying text is never touched by an accept-all action.
        assert entry["text"] == "the quick brown fox"
        assert entry["corrected_text"] is None
        assert entry["word_corrections"] is None
        assert entry["accepted"] is True

        # Recorded via the same correction_history mechanism as a real
        # correction, but with a distinguishing kind and no actual change.
        history = entry["correction_history"]
        assert history[0]["kind"] == "accept_all"
        assert history[0]["previous_text"] == "the quick brown fox"
        assert history[0]["new_text"] == "the quick brown fox"

        # An accept-all action is not a correction: it must not remove the
        # segment from needs_review by way of has_corrections()/WER.
        assert body["accuracy"]["has_corrections"] is False
        assert body["accuracy"]["word_error_rate"] is None

        # But it does clear the segment from the needs-review list.
        assert body["needs_review"] == []
        mock_session.commit.assert_called_once()

    def test_returns_422_when_already_accepted(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.4,
                "accepted": True,
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(f"/api/v1/jobs/{job.id}/segments/0/accept")
        assert response.status_code == 422

    def test_high_confidence_segment_not_in_needs_review_after_accept(
        self, client, as_current_user, mocker
    ):
        """Accepting a segment that was never low-confidence is harmless."""
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.99,
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        self._patch_session(client, mocker)

        try:
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/accept")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        assert response.json()["dialogue_entries"][0]["accepted"] is True


class TestRollbackSegment:
    def _patch_session(self, client, mocker):
        from transcribe_api.runtime.db_recording import get_session

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        return mock_session

    def test_returns_404_for_unknown_job(self, client, as_current_user, mocker):
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=None)
        response = client.post(f"/api/v1/jobs/{uuid.uuid4()}/segments/0/rollback")
        assert response.status_code == 404

    def test_returns_404_for_other_callers_job(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.user_id = uuid.uuid4()
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(f"/api/v1/jobs/{job.id}/segments/0/rollback")
        assert response.status_code == 404

    def test_returns_422_for_uncorrected_segment(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {"speaker": "0", "text": "the quick brown fox", "start_time": 0, "end_time": 1}
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(f"/api/v1/jobs/{job.id}/segments/0/rollback")
        assert response.status_code == 422

    def test_rolls_back_whole_segment_correction(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "corrected_text": "the slow brown fox",
                "correction_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "kind": "segment",
                        "previous_text": "the quick brown fox",
                        "new_text": "the slow brown fox",
                    }
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/rollback")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        entry = response.json()["dialogue_entries"][0]
        assert entry["corrected_text"] is None
        assert entry["word_corrections"] is None
        # A whole-section rollback is a hard reset — the log itself is
        # cleared too, rather than logging "yet another change".
        assert entry["correction_history"] is None
        mock_session.commit.assert_called_once()

    def test_rolls_back_word_range_correction(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "words": _words_payload("the", "quick", "brown", "fox"),
                "word_corrections": [{"start_word_index": 1, "end_word_index": 1, "text": "slow"}],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/rollback")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        entry = response.json()["dialogue_entries"][0]
        assert entry["corrected_text"] is None
        assert entry["word_corrections"] is None
        assert entry["correction_history"] is None
        mock_session.commit.assert_called_once()

    def test_rolls_back_an_accepted_segment(self, client, as_current_user, mocker):
        """A hard reset also un-accepts a segment, restoring it to needs_review."""
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "confidence": 0.4,
                "accepted": True,
                "correction_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "kind": "accept_all",
                        "previous_text": "the quick brown fox",
                        "new_text": "the quick brown fox",
                    }
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/rollback")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        body = response.json()
        entry = body["dialogue_entries"][0]
        assert entry["accepted"] is False
        assert entry["correction_history"] is None
        assert body["needs_review"] == [{"speaker": "0", "start_time": 0.0, "confidence": 0.4}]
        mock_session.commit.assert_called_once()


class TestRollbackToHistoryEntry:
    def _patch_session(self, client, mocker):
        from transcribe_api.runtime.db_recording import get_session

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        return mock_session

    def test_returns_404_for_unknown_job(self, client, as_current_user, mocker):
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=None)
        response = client.post(f"/api/v1/jobs/{uuid.uuid4()}/segments/0/history/0/rollback")
        assert response.status_code == 404

    def test_returns_404_for_other_callers_job(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.user_id = uuid.uuid4()
        job.dialogue_entries = [{"speaker": "0", "text": "hi", "start_time": 0, "end_time": 1}]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(f"/api/v1/jobs/{job.id}/segments/0/history/0/rollback")
        assert response.status_code == 404

    def test_returns_404_for_out_of_range_history_index(self, client, as_current_user, mocker):
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0,
                "end_time": 1,
                "corrected_text": "the slow brown fox",
                "correction_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "kind": "segment",
                        "previous_text": "the quick brown fox",
                        "new_text": "the slow brown fox",
                    }
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.post(f"/api/v1/jobs/{job.id}/segments/0/history/5/rollback")
        assert response.status_code == 404

    def test_rolls_back_to_a_specific_history_entry(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "corrected_text": "the slowest brown fox",
                "correction_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "kind": "segment",
                        "previous_text": "the quick brown fox",
                        "new_text": "the slow brown fox",
                    },
                    {
                        "timestamp": "2026-01-01T00:01:00+00:00",
                        "kind": "segment",
                        "previous_text": "the slow brown fox",
                        "new_text": "the slowest brown fox",
                    },
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            # Roll back to the state immediately before the second edit —
            # i.e. restore its previous_text, "the slow brown fox".
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/history/1/rollback")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        entry = response.json()["dialogue_entries"][0]
        assert entry["corrected_text"] == "the slow brown fox"
        assert entry["word_corrections"] is None
        assert len(entry["correction_history"]) == 3
        assert entry["correction_history"][2]["kind"] == "rollback"
        assert entry["correction_history"][2]["previous_text"] == "the slowest brown fox"
        assert entry["correction_history"][2]["new_text"] == "the slow brown fox"
        mock_session.commit.assert_called_once()

    def test_rollback_to_original_clears_corrected_text(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "corrected_text": "the slow brown fox",
                "correction_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "kind": "segment",
                        "previous_text": "the quick brown fox",
                        "new_text": "the slow brown fox",
                    }
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/history/0/rollback")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        entry = response.json()["dialogue_entries"][0]
        assert entry["corrected_text"] is None
        mock_session.commit.assert_called_once()

    def test_surgically_reverts_a_word_range_entry_to_the_original_word(
        self, client, as_current_user, mocker
    ):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "words": _words_payload("the", "quick", "brown", "fox"),
                "word_corrections": [{"start_word_index": 1, "end_word_index": 1, "text": "slow"}],
                "correction_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "kind": "word_range",
                        "previous_text": "the quick brown fox",
                        "new_text": "the slow brown fox",
                        "start_word_index": 1,
                        "end_word_index": 1,
                        "previous_phrase": "quick",
                        "new_phrase": "slow",
                    }
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/history/0/rollback")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        entry = response.json()["dialogue_entries"][0]
        # Reverting to the original word removes the correction entirely
        # rather than keeping a no-op WordCorrection whose text just
        # duplicates the original word.
        assert entry["word_corrections"] is None
        assert entry["corrected_text"] is None
        # Other untouched words' rendering data must survive intact.
        assert entry["words"] is not None
        new_history = entry["correction_history"][1]
        assert new_history["kind"] == "rollback"
        assert new_history["start_word_index"] == 1
        assert new_history["end_word_index"] == 1
        assert new_history["previous_phrase"] == "slow"
        assert new_history["new_phrase"] == "quick"
        mock_session.commit.assert_called_once()

    def test_surgically_reverts_a_re_edited_range_to_the_prior_correction(
        self, client, as_current_user, mocker
    ):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "words": _words_payload("the", "quick", "brown", "fox"),
                "word_corrections": [
                    {"start_word_index": 1, "end_word_index": 1, "text": "sluggish"}
                ],
                "correction_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "kind": "word_range",
                        "previous_text": "the quick brown fox",
                        "new_text": "the slow brown fox",
                        "start_word_index": 1,
                        "end_word_index": 1,
                        "previous_phrase": "quick",
                        "new_phrase": "slow",
                    },
                    {
                        "timestamp": "2026-01-01T00:01:00+00:00",
                        "kind": "word_range",
                        "previous_text": "the slow brown fox",
                        "new_text": "the sluggish brown fox",
                        "start_word_index": 1,
                        "end_word_index": 1,
                        "previous_phrase": "slow",
                        "new_phrase": "sluggish",
                    },
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            # Roll back to before the SECOND edit — should restore "slow"
            # (the first correction), not the original word "quick".
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/history/1/rollback")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        entry = response.json()["dialogue_entries"][0]
        assert entry["word_corrections"] == [
            {"start_word_index": 1, "end_word_index": 1, "text": "slow"}
        ]
        new_history = entry["correction_history"][2]
        assert new_history["previous_phrase"] == "sluggish"
        assert new_history["new_phrase"] == "slow"
        mock_session.commit.assert_called_once()

    def test_falls_back_to_flat_rollback_when_range_was_since_overridden(
        self, client, as_current_user, mocker
    ):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "words": _words_payload("the", "quick", "brown", "fox"),
                # A whole-segment freeform edit has since overridden
                # everything — the original word_range correction no longer
                # has a clean word-position correspondence to revert to.
                "corrected_text": "a completely different sentence",
                "correction_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "kind": "word_range",
                        "previous_text": "the quick brown fox",
                        "new_text": "the slow brown fox",
                        "start_word_index": 1,
                        "end_word_index": 1,
                        "previous_phrase": "quick",
                        "new_phrase": "slow",
                    },
                    {
                        "timestamp": "2026-01-01T00:01:00+00:00",
                        "kind": "segment",
                        "previous_text": "the slow brown fox",
                        "new_text": "a completely different sentence",
                    },
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/history/0/rollback")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        entry = response.json()["dialogue_entries"][0]
        # Falls back to the flat whole-segment snapshot rather than
        # attempting (and failing) a surgical per-word revert. Restoring
        # back to the original text clears corrected_text entirely, since
        # entry.text already reads "the quick brown fox".
        assert entry["corrected_text"] is None
        assert entry["word_corrections"] is None
        mock_session.commit.assert_called_once()

    def test_surgically_reverts_an_undo_when_range_is_currently_untouched(
        self, client, as_current_user, mocker
    ):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "words": _words_payload("the", "quick", "brown", "fox"),
                # The word_range correction below was already rolled back
                # (word_corrections is empty/None), so index 1 currently
                # reads the plain original word "quick".
                "word_corrections": None,
                "correction_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "kind": "word_range",
                        "previous_text": "the quick brown fox",
                        "new_text": "the slow brown fox",
                        "start_word_index": 1,
                        "end_word_index": 1,
                        "previous_phrase": "quick",
                        "new_phrase": "slow",
                    },
                    {
                        "timestamp": "2026-01-01T00:01:00+00:00",
                        "kind": "rollback",
                        "previous_text": "the slow brown fox",
                        "new_text": "the quick brown fox",
                        "start_word_index": 1,
                        "end_word_index": 1,
                        "previous_phrase": "slow",
                        "new_phrase": "quick",
                    },
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            # "Undo the undo" — roll back to before the rollback entry
            # itself, which should restore the "slow" correction rather
            # than falling back to a whole-segment flat snapshot just
            # because no WordCorrection currently exists for this range.
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/history/1/rollback")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        entry = response.json()["dialogue_entries"][0]
        assert entry["corrected_text"] is None
        assert entry["word_corrections"] == [
            {"start_word_index": 1, "end_word_index": 1, "text": "slow"}
        ]
        new_history = entry["correction_history"][2]
        assert new_history["kind"] == "rollback"
        assert new_history["previous_phrase"] == "quick"
        assert new_history["new_phrase"] == "slow"
        mock_session.commit.assert_called_once()

    def test_falls_back_to_flat_rollback_when_history_entry_has_no_previous_phrase(
        self, client, as_current_user, mocker
    ):
        from transcribe_api.runtime.db_recording import get_session

        # Legacy/malformed history entry: has word-range indices but no
        # previous_phrase (optional on CorrectionEntry). Attempting a
        # surgical revert here would try to create a WordCorrection with
        # text=None, which should never be allowed to reach that code path.
        job = _make_job(status=JobStatus.SUCCEEDED)
        job.caller_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        job.dialogue_entries = [
            {
                "speaker": "0",
                "text": "the quick brown fox",
                "start_time": 0.0,
                "end_time": 1.0,
                "words": _words_payload("the", "quick", "brown", "fox"),
                "word_corrections": [{"start_word_index": 1, "end_word_index": 1, "text": "slow"}],
                "correction_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "kind": "word_range",
                        "previous_text": "the quick brown fox",
                        "new_text": "the slow brown fox",
                        "start_word_index": 1,
                        "end_word_index": 1,
                    }
                ],
            }
        ]
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mock_session = self._patch_session(client, mocker)

        try:
            response = client.post(f"/api/v1/jobs/{job.id}/segments/0/history/0/rollback")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        entry = response.json()["dialogue_entries"][0]
        assert entry["corrected_text"] is None
        assert entry["word_corrections"] is None
        mock_session.commit.assert_called_once()


class TestListJobs:
    def test_returns_jobs(self, client, as_current_user, mocker):
        jobs = [_make_job(JobStatus.SUCCEEDED), _make_job(JobStatus.PENDING)]
        mocker.patch(
            "transcribe_api.api.routes_recording.list_jobs_paginated",
            return_value=(jobs, 2),
        )

        response = client.get("/api/v1/jobs")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert len(body["jobs"]) == 2
        assert body["limit"] == 20
        assert body["offset"] == 0

    def test_filters_by_status(self, client, as_current_user, mocker):
        mock = mocker.patch(
            "transcribe_api.api.routes_recording.list_jobs_paginated",
            return_value=([_make_job(JobStatus.SUCCEEDED)], 1),
        )

        response = client.get("/api/v1/jobs?status=succeeded")
        assert response.status_code == 200
        from transcribe_api.domain.models_recording import JobStatus as JS

        mock.assert_called_once_with(mocker.ANY, as_current_user.id, JS.SUCCEEDED, 20, 0)

    def test_rejects_invalid_status(self, client, as_current_user):
        response = client.get("/api/v1/jobs?status=notastate")
        assert response.status_code == 400

    def test_pagination_params_forwarded(self, client, as_current_user, mocker):
        mock = mocker.patch(
            "transcribe_api.api.routes_recording.list_jobs_paginated",
            return_value=([], 0),
        )

        response = client.get("/api/v1/jobs?limit=5&offset=10")
        assert response.status_code == 200
        mock.assert_called_once_with(mocker.ANY, as_current_user.id, None, 5, 10)

    def test_returns_empty_list_when_no_jobs(self, client, as_current_user, mocker):
        mocker.patch(
            "transcribe_api.api.routes_recording.list_jobs_paginated",
            return_value=([], 0),
        )

        response = client.get("/api/v1/jobs")
        assert response.status_code == 200
        body = response.json()
        assert body["jobs"] == []
        assert body["total"] == 0

    def test_requires_auth(self, client):
        response = client.get("/api/v1/jobs")
        assert response.status_code in (401, 422)


class TestDeleteJob:
    def test_returns_204(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job()
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session

        try:
            response = client.delete(f"/api/v1/jobs/{job.id}")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 204
        mock_session.delete.assert_called_once_with(job)
        mock_session.commit.assert_called_once()

    def test_legacy_job_returns_404_for_normal_user(self, client, as_current_user, mocker):
        # Pre-migration jobs (user_id=None) are restricted to SystemAdministrator.
        job = _make_job(user_id=None)
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        response = client.delete(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 404

    def test_legacy_job_deletable_by_system_administrator(self, client, as_admin_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job(user_id=None)
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session

        try:
            response = client.delete(f"/api/v1/jobs/{job.id}")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 204
        mock_session.delete.assert_called_once_with(job)

    def test_deletes_local_audio_blob(self, client, as_current_user, mocker, tmp_path, monkeypatch):
        from transcribe_api.runtime.db_recording import get_session

        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
        get_settings.cache_clear()

        job = _make_job()
        job.audio_blob_path = "uploads/caller-1/file.wav"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        delete_mock = mocker.patch("transcribe_api.api.routes_recording.local_storage.delete")

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        try:
            response = client.delete(f"/api/v1/jobs/{job.id}")
        finally:
            client.app.dependency_overrides.pop(get_session, None)
            get_settings.cache_clear()

        assert response.status_code == 204
        delete_mock.assert_called_once_with("uploads/caller-1/file.wav")
        mock_session.delete.assert_called_once_with(job)
        mock_session.commit.assert_called_once()

    def test_deletes_azure_audio_blob(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job()
        job.audio_blob_path = "uploads/caller-1/file.wav"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        manager = mocker.AsyncMock()
        manager.delete_blob = mocker.AsyncMock(return_value=True)
        manager.__aenter__ = mocker.AsyncMock(return_value=manager)
        manager.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("transcribe_api.api.routes_recording.AsyncAzureBlobManager", return_value=manager)

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        try:
            response = client.delete(f"/api/v1/jobs/{job.id}")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 204
        manager.delete_blob.assert_awaited_once_with("uploads/caller-1/file.wav")
        mock_session.delete.assert_called_once_with(job)

    def test_missing_azure_blob_still_deletes_row(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job()
        job.audio_blob_path = "uploads/caller-1/file.wav"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        manager = mocker.AsyncMock()
        manager.delete_blob = mocker.AsyncMock(return_value=False)  # not found
        manager.__aenter__ = mocker.AsyncMock(return_value=manager)
        manager.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("transcribe_api.api.routes_recording.AsyncAzureBlobManager", return_value=manager)

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        try:
            response = client.delete(f"/api/v1/jobs/{job.id}")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 204
        mock_session.delete.assert_called_once_with(job)

    def test_genuine_blob_error_returns_502_and_keeps_row(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job()
        job.audio_blob_path = "uploads/caller-1/file.wav"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)

        manager = mocker.AsyncMock()
        manager.delete_blob = mocker.AsyncMock(side_effect=RuntimeError("storage down"))
        manager.__aenter__ = mocker.AsyncMock(return_value=manager)
        manager.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("transcribe_api.api.routes_recording.AsyncAzureBlobManager", return_value=manager)

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        try:
            response = client.delete(f"/api/v1/jobs/{job.id}")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 502
        mock_session.delete.assert_not_called()
        mock_session.commit.assert_not_called()

    def test_deletes_batch_job_best_effort(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job()
        job.batch_job_url = "https://region.api.cognitive.microsoft.com/.../transcriptions/abc"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        delete_batch_job_mock = mocker.patch(
            "transcribe_api.api.routes_recording.delete_batch_job", new=mocker.AsyncMock()
        )

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        try:
            response = client.delete(f"/api/v1/jobs/{job.id}")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 204
        delete_batch_job_mock.assert_awaited_once_with(job.batch_job_url)
        mock_session.delete.assert_called_once_with(job)
        mock_session.commit.assert_called_once()

    def test_batch_job_deletion_failure_still_deletes_row(self, client, as_current_user, mocker):
        from transcribe_api.runtime.db_recording import get_session

        job = _make_job()
        job.batch_job_url = "https://region.api.cognitive.microsoft.com/.../transcriptions/abc"
        mocker.patch("transcribe_api.api.routes_recording.get_job_by_id", return_value=job)
        mocker.patch(
            "transcribe_api.api.routes_recording.delete_batch_job",
            new=mocker.AsyncMock(side_effect=RuntimeError("batch API down")),
        )

        mock_session = MagicMock()
        client.app.dependency_overrides[get_session] = lambda: mock_session
        try:
            response = client.delete(f"/api/v1/jobs/{job.id}")
        finally:
            client.app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 204
        mock_session.delete.assert_called_once_with(job)
        mock_session.commit.assert_called_once()
