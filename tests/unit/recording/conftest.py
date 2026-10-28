import os

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_CONNECTION_STRING", "postgresql://test:test@localhost:5432/test_db")
os.environ.setdefault("AZURE_SPEECH_KEY", "test-key")
os.environ.setdefault("AZURE_SPEECH_ENDPOINT", "https://test.cognitiveservices.azure.com")
os.environ.setdefault("AZURE_STORAGE_ACCOUNT_NAME", "teststorage")
os.environ.setdefault("AZURE_STORAGE_CONTAINER_NAME", "test-container")
os.environ.setdefault("LOCAL_API_KEY", "test-api-key")


@pytest.fixture(autouse=True)
def reset_settings_cache():
    from transcribe_api.runtime.settings_recording import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeBlobStorage:
    """Stands in for the Azure SDK clients AsyncAzureBlobManager builds.

    Records every BlobServiceClient and credential created and whether each
    was closed. close() awaits a real (if short) sleep before recording, the
    way aiohttp's session close does, so a close() run while the task is
    being cancelled only completes if it is shielded from that cancellation.
    """

    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        # Number of chunks served before the next one stalls forever, as a
        # slow storage read would; None never stalls.
        self.stall_after: int | None = None
        self.service_clients: list = []
        self.credentials: list = []

    @property
    def size(self) -> int:
        return sum(len(c) for c in self.chunks)

    @property
    def all_closed(self) -> bool:
        return all(c.closed for c in self.service_clients + self.credentials)

    def __repr__(self) -> str:
        services = [c.closed for c in self.service_clients]
        credentials = [c.closed for c in self.credentials]
        return f"FakeBlobStorage(service_clients_closed={services}, credentials_closed={credentials})"


@pytest.fixture
def fake_blob_storage(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from transcribe_api.runtime import blob_recording

    storage = FakeBlobStorage([b"chunk-0|", b"chunk-1|", b"chunk-2|"])

    async def _slow_close(resource):
        await asyncio.sleep(0.001)
        resource.closed = True

    class FakeCredential:
        def __init__(self):
            self.closed = False
            storage.credentials.append(self)

        async def close(self):
            await _slow_close(self)

    class FakeDownloader:
        async def chunks(self):
            for i, chunk in enumerate(storage.chunks):
                if storage.stall_after is not None and i >= storage.stall_after:
                    await asyncio.Event().wait()
                yield chunk

    class FakeBlobClient:
        async def get_blob_properties(self):
            return SimpleNamespace(size=storage.size, metadata={})

        async def download_blob(self, offset=None, length=None):
            return FakeDownloader()

    class FakeServiceClient:
        def __init__(self, *args, **kwargs):
            self.closed = False
            storage.service_clients.append(self)

        @classmethod
        def from_connection_string(cls, conn, **kwargs):
            return cls(**kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            await self.close()

        async def close(self):
            await _slow_close(self)

        def get_blob_client(self, container, blob):
            return FakeBlobClient()

    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "")
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "azure")
    monkeypatch.setattr(blob_recording, "AsyncBlobServiceClient", FakeServiceClient)
    monkeypatch.setattr(blob_recording, "DefaultAzureCredential", FakeCredential)
    return storage
