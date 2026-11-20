"""DIAAT-408: storing and reading back an upload's original and its WAV (stt/audio_storage.py and the
blob manager methods behind it). No network: the Azure SDK is driven through a fake HTTP transport, so
what is checked is the SDK's real behaviour (parallel blocks) rather than a mock's."""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

import pytest
from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport
from azure.storage.blob.aio import BlobServiceClient

from transcribe_api.runtime import blob_recording, settings_recording
from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager
from transcribe_api.stt import audio_storage, local_storage
from transcribe_api.stt.audio_storage import (
    AzureAudioStorage,
    LocalAudioStorage,
    StorageError,
    source_blob_name,
    source_extension,
)

# Azurite's published development account key (as in tests/unit/dictation/.../test_azurite_storage.py): a
# connection string with a key, so the SDK signs requests locally. Nothing is ever sent: the transport
# below answers every request itself.
CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"  # gitleaks:allow
    "BlobEndpoint=http://127.0.0.1:1/devstoreaccount1;"
)
BLOCK = 4 * 1024 * 1024


class _Response(AsyncHttpResponse):
    def __init__(self, request):
        self.request = request
        self.status_code = 201
        self.headers = {
            "ETag": '"0x1"',
            "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            "x-ms-request-server-encrypted": "true",
        }
        self.reason = "Created"
        self.content_type = None
        self.block_size = 4096
        self.internal_response = None
        self.is_closed = True
        self.is_stream_consumed = True

    def body(self):
        return b""

    def text(self, encoding=None):
        return ""

    async def load_body(self):
        pass

    async def read(self):
        return b""

    def stream_download(self, pipeline, **kwargs):
        raise NotImplementedError

    async def __aexit__(self, *args):
        pass

    async def close(self):
        pass


class CountingTransport(AsyncHttpTransport):
    """Answers every request with 201 after a short delay, and records how many were in flight at once."""

    def __init__(self, hold_until: int = 0) -> None:
        self.in_flight = 0
        self.peak = 0
        self.requests: list[str] = []
        # Block every request until this many are in flight together (no sleeping): the SDK's parallelism
        # is then observed, not timed. 0: answer at once.
        self.hold_until = hold_until
        self._together = asyncio.Event()

    async def open(self):
        pass

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def send(self, request, **kwargs):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.requests.append(request.method)
        if self.hold_until:
            if self.in_flight >= self.hold_until:
                self._together.set()
            # The last blocks and the block list come when fewer than hold_until are left: let them through.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._together.wait(), 2)
        await asyncio.sleep(0)
        self.in_flight -= 1
        return _Response(request)


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", CONNECTION_STRING)
    settings_recording.get_settings.cache_clear()
    transport = CountingTransport()

    class Client:
        @staticmethod
        def from_connection_string(connection_string, **kwargs):
            return BlobServiceClient.from_connection_string(connection_string, transport=transport, **kwargs)

    monkeypatch.setattr(blob_recording, "AsyncBlobServiceClient", Client)
    return transport


async def _upload(tmp_path, blocks: int, **kwargs) -> bool:
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"x" * (BLOCK * blocks))
    async with AsyncAzureBlobManager() as manager:
        return await manager.create_blob_from_file(path, "incoming/u/x.mp3", **kwargs)


async def test_blocks_are_sent_in_parallel_when_asked_to(transport, tmp_path):
    transport.hold_until = 8  # every request waits until eight are in flight together: the SDK's limit is observed
    assert await _upload(tmp_path, 20, max_concurrency=8)
    assert transport.peak == 8  # the SDK's max_concurrency, at its memory cost of 8 x 4 MiB
    assert transport.requests.count("PUT") == 21  # 20 blocks and the block list


async def test_without_a_concurrency_the_upload_is_as_it_was_one_block_at_a_time(transport, tmp_path):
    assert await _upload(tmp_path, 8)
    assert transport.peak == 1


async def test_the_upload_that_converts_in_the_request_is_unchanged_by_the_new_parameter(transport, tmp_path):
    assert await _upload(tmp_path, 3, max_concurrency=None)
    assert transport.peak == 1


# The blob manager's reads -----------------------------------------------------------------------


class _Container:
    def __init__(self, names, error=None):
        self.names, self.error, self.prefixes = names, error, []

    async def list_blobs(self, name_starts_with=None, **kwargs):
        self.prefixes.append(name_starts_with)
        if self.error:
            raise self.error
        for name in self.names:
            yield type("Blob", (), {"name": name})()


class _Service:
    def __init__(self, container):
        self.container = container

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get_container_client(self, name):
        return self.container


async def test_blob_names_are_listed_by_prefix_and_a_storage_failure_is_raised_not_hidden(monkeypatch):
    container = _Container(["incoming/u/a.mp3", "incoming/u/a.mp3.tmp"])
    manager = AsyncAzureBlobManager()
    monkeypatch.setattr(manager, "_blob_service_client", lambda **kw: _Service(container))
    assert await manager.find_blob_names("incoming/u/a") == ["incoming/u/a.mp3", "incoming/u/a.mp3.tmp"]
    assert container.prefixes == ["incoming/u/a"]

    # list_blobs_in_prefix answers [] for a failure, which a caller cannot tell from "no such blob".
    broken = _Container([], error=OSError("storage down"))
    monkeypatch.setattr(manager, "_blob_service_client", lambda **kw: _Service(broken))
    assert await manager.list_blobs_in_prefix("incoming/u/a") == []
    with pytest.raises(OSError, match="storage down"):
        await manager.find_blob_names("incoming/u/a")


class _Downloader:
    """Offers chunks() only: a download that read the whole blob into memory (readall) would fail."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def chunks(self):
        for chunk in self._chunks:
            yield chunk


class _BlobClient:
    def __init__(self, chunks, exists=True, error=None):
        self._chunks, self._exists, self._error = chunks, exists, error

    async def exists(self):
        return self._exists

    async def download_blob(self):
        if self._error:
            raise self._error
        return _Downloader(self._chunks)


class _DownloadService(_Service):
    def __init__(self, blob):
        self.blob = blob

    def get_blob_client(self, container, blob):
        return self.blob


async def test_a_download_is_written_a_chunk_at_a_time(monkeypatch, tmp_path):
    manager = AsyncAzureBlobManager()
    monkeypatch.setattr(
        manager, "_blob_service_client", lambda **kw: _DownloadService(_BlobClient([b"abc", b"def", b"g"]))
    )
    target = tmp_path / "out.mp3"
    assert await manager.download_blob_to_file("incoming/u/a.mp3", target) is True
    assert target.read_bytes() == b"abcdefg"


async def test_a_download_of_a_blob_that_is_not_there_raises_not_found_and_other_failures_return_false(
    monkeypatch, tmp_path
):
    manager = AsyncAzureBlobManager()
    monkeypatch.setattr(manager, "_blob_service_client", lambda **kw: _DownloadService(_BlobClient([], exists=False)))
    with pytest.raises(FileNotFoundError):
        await manager.download_blob_to_file("incoming/u/a.mp3", tmp_path / "x")
    monkeypatch.setattr(
        manager, "_blob_service_client", lambda **kw: _DownloadService(_BlobClient([], error=OSError("x")))
    )
    assert await manager.download_blob_to_file("incoming/u/a.mp3", tmp_path / "x") is False


# The storage the service uses ---------------------------------------------------------------------


def test_the_original_is_named_by_user_upload_and_extension():
    user, upload = uuid4(), uuid4()
    assert source_blob_name(user, upload, ".m4a") == f"incoming/{user}/{upload}.m4a"
    assert source_extension(f"incoming/{user}/{upload}.M4A") == ".m4a"
    assert source_extension("incoming/u/noextension") == ""


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "http://api.test")
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    settings_recording.get_settings.cache_clear()
    return LocalAudioStorage()


@pytest.mark.parametrize("extension", sorted(audio_storage.AUDIO_EXTENSIONS))
async def test_the_local_storage_finds_an_original_whatever_its_extension(local, extension):
    user, upload = uuid4(), uuid4()
    assert await local.find_source(user, upload) is None
    local_storage.save(b"x", source_blob_name(user, upload, extension))
    assert await local.find_source(user, upload) == source_blob_name(user, upload, extension)
    assert await local.find_source(uuid4(), upload) is None  # another user's upload id is not found


async def test_the_local_storage_round_trips_a_file_and_forgives_a_missing_delete(local, tmp_path):
    source = tmp_path / "in.bin"
    source.write_bytes(b"audio")
    assert await local.upload(source, "uploads/u/x.wav", concurrency=8) is True
    target = tmp_path / "out.bin"
    await local.download("uploads/u/x.wav", target)
    assert target.read_bytes() == b"audio"
    await local.delete("uploads/u/x.wav")
    await local.delete("uploads/u/x.wav")  # already gone: not an error
    with pytest.raises(FileNotFoundError):
        await local.download("uploads/u/x.wav", target)
    assert (await local.url("uploads/u/x.wav")).startswith(
        "http://api.test/api/v1/local-audio/uploads/u/x.wav?expires="
    )


async def test_the_storage_follows_the_setting(local, monkeypatch):
    assert isinstance(audio_storage.get_audio_storage(), LocalAudioStorage)
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "azure")
    settings_recording.get_settings.cache_clear()
    assert isinstance(audio_storage.get_audio_storage(), AzureAudioStorage)


async def test_azure_storage_reports_a_failed_download_as_a_storage_error_and_a_missing_blob_as_not_found(
    mocker, tmp_path
):
    manager = mocker.AsyncMock()
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch.object(audio_storage, "AsyncAzureBlobManager", return_value=manager)
    manager.download_blob_to_file = mocker.AsyncMock(return_value=False)
    with pytest.raises(StorageError):
        await AzureAudioStorage().download("incoming/u/a.mp3", tmp_path / "x")
    manager.download_blob_to_file = mocker.AsyncMock(side_effect=FileNotFoundError("Blob not found"))
    with pytest.raises(FileNotFoundError):
        await AzureAudioStorage().download("incoming/u/a.mp3", tmp_path / "x")


async def test_azure_storage_finds_nothing_when_the_prefix_has_no_original(mocker):
    manager = mocker.AsyncMock()
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    manager.find_blob_names = mocker.AsyncMock(return_value=[])
    mocker.patch.object(audio_storage, "AsyncAzureBlobManager", return_value=manager)
    assert await AzureAudioStorage().find_source(uuid4(), uuid4()) is None
