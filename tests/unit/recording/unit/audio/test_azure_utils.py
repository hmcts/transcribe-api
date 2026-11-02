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


class TestStreamBlobRange:
    """stream_blob_range closes its BlobServiceClient however streaming ends.

    GET /jobs/{id}/audio streams through it, and a browser's audio player
    routinely abandons range requests partway, so the early-exit paths matter
    as much as running to completion. An unclosed client leaks an aiohttp
    session and its TLS connections to storage.
    """

    @pytest.mark.asyncio
    async def test_closes_service_client_after_full_stream(self, fake_blob_storage):
        from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager

        manager = AsyncAzureBlobManager()
        chunks = [c async for c in manager.stream_blob_range("a.wav", 0, fake_blob_storage.size)]
        await manager.close()

        assert chunks == fake_blob_storage.chunks
        assert len(fake_blob_storage.service_clients) == 1
        assert fake_blob_storage.all_closed

    @pytest.mark.asyncio
    async def test_closes_service_client_when_closed_partway(self, fake_blob_storage):
        from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager

        manager = AsyncAzureBlobManager()
        stream = manager.stream_blob_range("a.wav", 0, fake_blob_storage.size)
        assert await anext(stream) == b"chunk-0|"
        await stream.aclose()
        await manager.close()

        assert fake_blob_storage.all_closed

    @pytest.mark.asyncio
    async def test_closes_service_client_when_cancelled_mid_read(self, fake_blob_storage):
        """Starlette cancels a streaming response on client disconnect.

        It does so through an anyio cancel scope, which re-cancels every await
        made inside it until the scope exits, so cleanup that awaits (closing
        an aiohttp session does) is cut short unless it is shielded.
        """
        import anyio

        from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager

        fake_blob_storage.stall_after = 1
        manager = AsyncAzureBlobManager()
        received = []

        async def consume():
            async for chunk in manager.stream_blob_range("a.wav", 0, fake_blob_storage.size):
                received.append(chunk)

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            while not received:
                await anyio.lowlevel.checkpoint()
            tg.cancel_scope.cancel()

        assert received == [b"chunk-0|"]
        (service_client,) = fake_blob_storage.service_clients
        assert service_client.closed


class TestManagerClose:
    @pytest.mark.asyncio
    async def test_closes_credential_inside_a_cancelled_scope(self, fake_blob_storage):
        """close() runs as cleanup after a disconnect, i.e. while being cancelled."""
        import anyio

        from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager

        manager = AsyncAzureBlobManager()
        with anyio.CancelScope() as scope:
            scope.cancel()
            await manager.close()

        (credential,) = fake_blob_storage.credentials
        assert credential.closed
