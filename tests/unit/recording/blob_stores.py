"""Stand-ins for Azure Blob Storage in the recording tests, behind one small store interface
(``put``, ``get``, ``names``, ``metadata``, ``delete``, ``in``), so a test arranges and inspects blobs the same way
on either.

``MemoryBlobStorage`` replaces the Azure SDK's clients in-process, for unit and functional tests. Its behaviour is
held to Azurite's by tests/unit/recording/unit/audio/test_blob_manager_contract.py. ``AzuriteBlobStorage`` is
Microsoft's storage emulator in a container, for integration and end-to-end tests.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError

# The version and arguments of the preview chart's Azurite (charts/transcribe-api), so CI and previews agree.
AZURITE_IMAGE = os.environ.get("TEST_AZURITE_IMAGE", "mcr.microsoft.com/azure-storage/azurite:3.33.0")
AZURITE_COMMAND = "azurite-blob --blobHost 0.0.0.0 --loose --skipApiVersionCheck --disableProductStyleUrl"
AZURITE_BLOB_PORT = 10000
AZURITE_ACCOUNT = "devstoreaccount1"
AZURITE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="  # gitleaks:allow


def install_memory_blob_storage(monkeypatch: Any) -> MemoryBlobStorage:
    """Put a ``MemoryBlobStorage`` in place of the Azure SDK clients the recording code uses."""
    from transcribe_api.runtime import blob_recording, settings_recording

    storage_settings(monkeypatch, connection_string="")
    storage = MemoryBlobStorage([settings_recording.get_settings().AZURE_STORAGE_CONTAINER_NAME])
    monkeypatch.setattr(blob_recording, "AsyncBlobServiceClient", storage.service_client_class())
    monkeypatch.setattr(blob_recording, "DefaultAzureCredential", storage.credential_class())
    return storage


def storage_settings(monkeypatch: Any, *, connection_string: str, container: str | None = None) -> None:
    """Azure storage settings for a stand-in, and the real blob manager behind the audio storage again: the root
    conftest's azure_audio_storage_is_never_reached guards it, and a stand-in is what makes reaching it safe."""
    from transcribe_api.runtime import blob_recording, settings_dictation, settings_recording
    from transcribe_api.stt import audio_storage

    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "azure")
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", connection_string)
    if container is not None:
        monkeypatch.setenv("AZURE_STORAGE_CONTAINER_NAME", container)
    settings_recording.get_settings.cache_clear()
    settings_dictation.get_settings.cache_clear()
    monkeypatch.setattr(audio_storage, "AsyncAzureBlobManager", blob_recording.AsyncAzureBlobManager)


def installed_memory_blob_storage() -> MemoryBlobStorage:
    """The ``MemoryBlobStorage`` the recording code is using now (``install_memory_blob_storage``)."""
    from transcribe_api.runtime import blob_recording

    storage = getattr(blob_recording.AsyncBlobServiceClient, "memory_storage", None)
    if storage is None:
        raise RuntimeError("No MemoryBlobStorage is installed: use the memory_blob_storage fixture.")
    return storage


def start_azurite() -> Any:
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    container = (
        DockerContainer(AZURITE_IMAGE)
        .with_command(AZURITE_COMMAND)
        .with_exposed_ports(AZURITE_BLOB_PORT)
        .waiting_for(LogMessageWaitStrategy(re.compile(r"Azurite Blob service (is )?successfully listen")))
    )
    container.start()
    return container


def azurite_connection_string_for(host: str, port: int | str) -> str:
    return (
        f"DefaultEndpointsProtocol=http;AccountName={AZURITE_ACCOUNT};AccountKey={AZURITE_KEY};"
        f"BlobEndpoint=http://{host}:{port}/{AZURITE_ACCOUNT};"
    )


class AzuriteBlobStorage:
    """Containers of their own in an Azurite, removed by ``remove``."""

    def __init__(self, connection_string: str, containers: list[str]):
        from azure.storage.blob import BlobServiceClient

        self._service = BlobServiceClient.from_connection_string(connection_string)
        self.containers = list(containers)
        self.container = self.containers[0]
        for name in self.containers:
            self._service.create_container(name)

    def put(self, name: str, content: bytes, *, container: str | None = None, metadata: dict | None = None) -> None:
        self._blob(name, container).upload_blob(content, overwrite=True, metadata=metadata)

    def get(self, name: str, *, container: str | None = None) -> bytes | None:
        try:
            return self._blob(name, container).download_blob().readall()
        except ResourceNotFoundError:
            return None

    def names(self, prefix: str = "", *, container: str | None = None) -> list[str]:
        client = self._service.get_container_client(container or self.container)
        return [blob.name for blob in client.list_blobs(name_starts_with=prefix or None)]

    def metadata(self, name: str, *, container: str | None = None) -> dict[str, str] | None:
        try:
            return dict(self._blob(name, container).get_blob_properties().metadata)
        except ResourceNotFoundError:
            return None

    def delete(self, name: str, *, container: str | None = None) -> None:
        try:
            self._blob(name, container).delete_blob()
        except ResourceNotFoundError:
            pass

    def __contains__(self, name: str) -> bool:
        return self._blob(name, None).exists()

    def remove(self) -> None:
        for name in self.containers:
            self._service.delete_container(name)
        self._service.close()

    def _blob(self, name: str, container: str | None) -> Any:
        return self._service.get_blob_client(container or self.container, name)


@dataclass
class StoredBlob:
    content: bytes
    metadata: dict[str, str] = field(default_factory=dict)
    content_type: str | None = None
    last_modified: datetime = field(default_factory=lambda: datetime.now(UTC))


class MemoryBlobStorage:
    """Azure Blob Storage in memory, in place of the SDK clients ``runtime/blob_recording.py`` builds.

    ``fail_next(operation, error)`` makes the next call of an SDK operation (``upload_blob``,
    ``download_blob``, ...) raise ``error``; every client and credential made is kept, with whether it was closed.
    """

    def __init__(self, containers: list[str]):
        self.containers: dict[str, dict[str, StoredBlob]] = {name: {} for name in containers}
        self.container = containers[0]
        self.download_chunk_bytes = 4 * 1024 * 1024
        self.service_clients: list[_ServiceClient] = []
        self.credentials: list[_Credential] = []
        self._failures: dict[str, Exception] = {}

    def put(self, name: str, content: bytes, *, container: str | None = None, metadata: dict | None = None) -> None:
        self._blobs(container or self.container)[name] = StoredBlob(bytes(content), dict(metadata or {}))

    def get(self, name: str, *, container: str | None = None) -> bytes | None:
        blob = self._blobs(container or self.container).get(name)
        return None if blob is None else blob.content

    def names(self, prefix: str = "", *, container: str | None = None) -> list[str]:
        return sorted(name for name in self._blobs(container or self.container) if name.startswith(prefix))

    def metadata(self, name: str, *, container: str | None = None) -> dict[str, str] | None:
        blob = self._blobs(container or self.container).get(name)
        return None if blob is None else dict(blob.metadata)

    def delete(self, name: str, *, container: str | None = None) -> None:
        self._blobs(container or self.container).pop(name, None)

    def __contains__(self, name: str) -> bool:
        return name in self._blobs(self.container)

    def fail_next(self, operation: str, error: Exception) -> None:
        self._failures[operation] = error

    @property
    def all_closed(self) -> bool:
        return all(client.closed for client in [*self.service_clients, *self.credentials])

    def service_client_class(self) -> type:
        storage = self

        class ServiceClient(_ServiceClient):
            memory_storage = storage

            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(storage)

            @classmethod
            def from_connection_string(cls, connection_string: str, **kwargs: Any) -> ServiceClient:
                return cls()

        return ServiceClient

    def credential_class(self) -> type:
        storage = self

        class Credential(_Credential):
            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(storage)

        return Credential

    def _raise_if_failing(self, operation: str) -> None:
        error = self._failures.pop(operation, None)
        if error is not None:
            raise error

    def _blobs(self, container: str) -> dict[str, StoredBlob]:
        try:
            return self.containers[container]
        except KeyError:
            raise ResourceNotFoundError("The specified container does not exist.") from None


class _Credential:
    def __init__(self, storage: MemoryBlobStorage):
        self.closed = False
        storage.credentials.append(self)

    async def close(self) -> None:
        self.closed = True


class _ServiceClient:
    def __init__(self, storage: MemoryBlobStorage):
        self._storage = storage
        self.closed = False
        storage.service_clients.append(self)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self.closed = True

    def get_container_client(self, container: str) -> _ContainerClient:
        return _ContainerClient(self._storage, container)

    def get_blob_client(self, container: str, blob: str) -> _BlobClient:
        return _BlobClient(self._storage, container, blob)


class _ContainerClient:
    def __init__(self, storage: MemoryBlobStorage, name: str):
        self._storage = storage
        self._name = name

    async def get_container_properties(self, **kwargs: Any) -> SimpleNamespace:
        self._storage._raise_if_failing("get_container_properties")
        self._storage._blobs(self._name)
        return SimpleNamespace(name=self._name)

    def list_blobs(self, name_starts_with: str | None = None, include: list[str] | None = None, **kwargs: Any):
        return self._list(name_starts_with or "", include or [])

    async def _list(self, prefix: str, include: list[str]):
        self._storage._raise_if_failing("list_blobs")
        blobs = self._storage._blobs(self._name)
        for name in sorted(blobs):
            if name.startswith(prefix):
                blob = blobs[name]
                yield SimpleNamespace(
                    name=name,
                    size=len(blob.content),
                    last_modified=blob.last_modified,
                    metadata=dict(blob.metadata) if "metadata" in include else {},
                )


class _BlobClient:
    def __init__(self, storage: MemoryBlobStorage, container: str, name: str):
        self._storage = storage
        self._container = container
        self._name = name

    async def upload_blob(
        self,
        data: Any,
        overwrite: bool = False,
        content_settings: Any = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> dict:
        self._storage._raise_if_failing("upload_blob")
        blobs = self._storage._blobs(self._container)
        if not overwrite and self._name in blobs:
            raise ResourceExistsError("The specified blob already exists.")
        content = data if isinstance(data, bytes | bytearray | str) else data.read()
        content = content.encode() if isinstance(content, str) else bytes(content)
        content_type = getattr(content_settings, "content_type", None)
        blobs[self._name] = StoredBlob(content, dict(metadata or {}), content_type)
        return {"last_modified": blobs[self._name].last_modified}

    async def delete_blob(self, delete_snapshots: str | None = None, **kwargs: Any) -> None:
        self._storage._raise_if_failing("delete_blob")
        self._stored()
        del self._storage._blobs(self._container)[self._name]

    async def exists(self, **kwargs: Any) -> bool:
        self._storage._raise_if_failing("exists")
        return self._name in self._storage.containers.get(self._container, {})

    async def get_blob_properties(self, **kwargs: Any) -> SimpleNamespace:
        self._storage._raise_if_failing("get_blob_properties")
        blob = self._stored()
        return SimpleNamespace(
            name=self._name,
            size=len(blob.content),
            metadata=dict(blob.metadata),
            last_modified=blob.last_modified,
            content_settings=SimpleNamespace(content_type=blob.content_type),
        )

    async def set_blob_metadata(self, metadata: dict | None = None, **kwargs: Any) -> dict:
        self._storage._raise_if_failing("set_blob_metadata")
        blob = self._stored()
        blob.metadata = dict(metadata or {})
        return {"last_modified": blob.last_modified}

    async def download_blob(self, offset: int | None = None, length: int | None = None, **kwargs: Any) -> _Downloader:
        self._storage._raise_if_failing("download_blob")
        content = self._stored().content
        start = offset or 0
        if offset is not None and start >= len(content):
            error = HttpResponseError(message="The range specified is invalid for the current size of the resource.")
            error.status_code = 416
            error.error_code = "InvalidRange"
            raise error
        end = len(content) if length is None else min(start + length, len(content))
        return _Downloader(content[start:end], self._storage.download_chunk_bytes)

    def _stored(self) -> StoredBlob:
        blob = self._storage._blobs(self._container).get(self._name)
        if blob is None:
            raise ResourceNotFoundError("The specified blob does not exist.")
        return blob


class _Downloader:
    def __init__(self, content: bytes, chunk_bytes: int):
        self._content = content
        self._chunk_bytes = chunk_bytes
        self.size = len(content)

    async def readall(self) -> bytes:
        return self._content

    async def chunks(self):
        for start in range(0, len(self._content), self._chunk_bytes):
            yield self._content[start : start + self._chunk_bytes]
