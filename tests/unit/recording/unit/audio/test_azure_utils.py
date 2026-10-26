"""Unit tests for Azure Storage utility helpers."""

import pytest

from transcribe_api.runtime.blob_recording import _sanitize_for_log


class TestSanitizeForLog:
    def test_passes_through_plain_values(self):
        assert _sanitize_for_log("audio-processing/uploads/file.wav") == (
            "'audio-processing/uploads/file.wav'"
        )

    def test_escapes_newlines_that_would_forge_log_lines(self):
        malicious = "file.wav\n2026-01-01 ERROR fake log line injected"
        sanitized = _sanitize_for_log(malicious)
        assert "\n" not in sanitized
        assert "\\n" in sanitized

    def test_escapes_carriage_returns_and_tabs(self):
        sanitized = _sanitize_for_log("a\r\nb\tc")
        assert "\r" not in sanitized
        assert "\n" not in sanitized
        assert "\t" not in sanitized

    def test_stringifies_non_string_values(self):
        assert _sanitize_for_log(ValueError("boom")) == "'boom'"


class TestCreateBlobFromFile:
    """A file is uploaded in blocks, never read whole into memory.

    The SDK's default single-put threshold is 64 MiB: anything smaller is read
    whole and sent in one request, which for a recording's WAV (about 115 MB per
    hour of audio) meant up to 64 MiB extra per upload in the API pod.
    """

    @pytest.fixture
    def clients(self, monkeypatch):
        from transcribe_api.runtime import blob_recording

        created = []

        class FakeBlobClient:
            def __init__(self):
                self.uploaded = None

            async def upload_blob(self, data, **kwargs):
                self.uploaded = data.read()

        class FakeServiceClient:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs
                self.blob_client = FakeBlobClient()
                created.append(self)

            @classmethod
            def from_connection_string(cls, conn, **kwargs):
                return cls(**kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get_blob_client(self, container, blob):
                return self.blob_client

        monkeypatch.setattr(blob_recording, "AsyncBlobServiceClient", FakeServiceClient)
        monkeypatch.setattr(blob_recording, "DefaultAzureCredential", lambda: None)
        return created

    @pytest.mark.asyncio
    @pytest.mark.parametrize("connection_string", ["", "UseDevelopmentStorage=true"])
    async def test_uploads_in_bounded_blocks(self, clients, tmp_path, monkeypatch, connection_string):
        from transcribe_api.runtime import blob_recording
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", connection_string)
        get_settings.cache_clear()
        try:
            source = tmp_path / "output.wav"
            source.write_bytes(b"WAVDATA")

            ok = await blob_recording.AsyncAzureBlobManager().create_blob_from_file(source, "uploads/x/a.wav")
        finally:
            get_settings.cache_clear()

        assert ok is True
        (client,) = clients
        assert client.blob_client.uploaded == b"WAVDATA"
        assert client.kwargs["max_single_put_size"] <= 4 * 1024 * 1024
        assert client.kwargs["max_block_size"] <= 4 * 1024 * 1024
