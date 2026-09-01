"""Unit tests for the dev-only local audio storage backend."""

import hashlib

import pytest

from transcribe_api.stt import local_storage
from transcribe_api.runtime.settings_recording import get_settings


@pytest.fixture(autouse=True)
def local_storage_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


class TestSaveAndRead:
    def test_round_trips_bytes(self):
        local_storage.save(b"fake-audio-bytes", "uploads/caller-1/file.wav")
        assert local_storage.read("uploads/caller-1/file.wav") == b"fake-audio-bytes"

    def test_stores_hierarchical_blob_names_as_a_hashed_flat_file(self, local_storage_dir):
        local_storage.save(b"x", "a/b/c/file.mp3")
        digest = hashlib.sha256(b"a/b/c/file.mp3").hexdigest()
        assert (local_storage_dir / digest).exists()

    def test_rejects_path_traversal_on_save(self):
        with pytest.raises(ValueError, match="invalid blob_name"):
            local_storage.save(b"x", "../../etc/passwd")

    def test_rejects_path_traversal_on_read(self):
        with pytest.raises(ValueError, match="invalid blob_name"):
            local_storage.read("../../etc/passwd")

    def test_read_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            local_storage.read("does/not/exist.wav")


class TestSizeAndReadRange:
    def test_size_returns_byte_count(self):
        local_storage.save(b"0123456789", "uploads/caller-1/file.wav")
        assert local_storage.size("uploads/caller-1/file.wav") == 10

    def test_size_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            local_storage.size("does/not/exist.wav")

    def test_read_range_returns_requested_slice(self):
        local_storage.save(b"0123456789", "uploads/caller-1/file.wav")
        assert local_storage.read_range("uploads/caller-1/file.wav", 2, 3) == b"234"

    def test_read_range_from_start(self):
        local_storage.save(b"0123456789", "uploads/caller-1/file.wav")
        assert local_storage.read_range("uploads/caller-1/file.wav", 0, 4) == b"0123"

    def test_read_range_missing_file_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            local_storage.read_range("does/not/exist.wav", 0, 4)


class TestValidateBlobName:
    @pytest.mark.parametrize(
        "blob_name",
        [
            "../../etc/passwd",
            "uploads/../../../etc/passwd",
            "uploads/./file.wav",
            "uploads//file.wav",
            "uploads/caller$1/file.wav",
            "uploads/caller 1/file.wav",
        ],
    )
    def test_rejects_unsafe_blob_names(self, blob_name):
        with pytest.raises(ValueError, match="invalid blob_name"):
            local_storage.save(b"x", blob_name)

    @pytest.mark.parametrize(
        "blob_name",
        [
            "file.wav",
            "uploads/caller-1/file.wav",
            "uploads/00000000-0000-0000-0000-000000000001/hearing.mp3",
        ],
    )
    def test_accepts_safe_blob_names(self, blob_name):
        local_storage.save(b"x", blob_name)
        assert local_storage.read(blob_name) == b"x"


class TestBuildUrl:
    def test_builds_url_from_base(self, monkeypatch):
        monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "https://abc123.ngrok-free.app")
        get_settings.cache_clear()

        url = local_storage.build_url("uploads/caller-1/file.wav")

        assert url == "https://abc123.ngrok-free.app/api/v1/local-audio/uploads/caller-1/file.wav"

    def test_raises_when_base_url_not_configured(self, monkeypatch):
        monkeypatch.delenv("LOCAL_AUDIO_BASE_URL", raising=False)
        get_settings.cache_clear()

        with pytest.raises(ValueError, match="LOCAL_AUDIO_BASE_URL"):
            local_storage.build_url("uploads/caller-1/file.wav")


class TestDelete:
    def test_removes_stored_file(self, local_storage_dir):
        local_storage.save(b"audio", "uploads/caller-1/file.wav")
        local_storage.delete("uploads/caller-1/file.wav")
        with pytest.raises(FileNotFoundError):
            local_storage.read("uploads/caller-1/file.wav")

    def test_missing_file_is_a_noop(self):
        # Idempotent: deleting a file that was never stored must not raise.
        local_storage.delete("uploads/caller-1/never-existed.wav")

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="invalid blob_name"):
            local_storage.delete("../../etc/passwd")
