"""Where the original and the converted audio of an upload are kept (DIAAT-408).

The upload request stores the original (``incoming/{user_id}/{upload_id}{ext}``); the worker
(stt/preparation_service.py) reads it back, stores the converted WAV at the blob name the upload
answered with, and deletes the original. Both backends of ``AUDIO_STORAGE_BACKEND`` sit behind one
small interface so that code is written once: Azure Blob Storage (``AsyncAzureBlobManager``), and
the dev-only local directory (``local_storage``).

Storage failures are exceptions here (the blob manager's own methods report most of them as
``False`` or an empty list, which would read as "no such upload"): a caller that cannot tell
"not there" from "could not look" must not treat the second as the first.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Protocol
from uuid import UUID

from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.stt import local_storage

# Where an upload's original waits for the worker. Under its own prefix, so a lifecycle rule can
# discard what no job ever claimed, and apart from ``uploads/{user_id}/``, the prefix the
# ownership check of POST /jobs trusts.
SOURCE_PREFIX = "incoming"
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".mp4", ".m4a", ".ogg", ".flac"})


class StorageError(Exception):
    """The storage backend could not do what was asked (not: the blob is not there)."""


def source_blob_name(user_id: UUID | str, upload_id: UUID | str, extension: str) -> str:
    """The blob an upload's original is stored as: ``incoming/{user_id}/{upload_id}{extension}``."""
    return f"{SOURCE_PREFIX}/{user_id}/{upload_id}{extension}"


def job_wav_name(upload_blob_name: str, job_id: UUID | str) -> str:
    """The WAV a deferred job's worker stores, named from the job as well as the upload:
    ``uploads/{user_id}/{upload_id}-{stem}.wav`` becomes ``uploads/{user_id}/{upload_id}-{job_id}-{stem}.wav``.

    A job made from an upload used to share every blob with any other job made from it (the WAV and the
    parts named from it): one job's submission overwrote, and its cleanup or delete removed, the other's.
    Still under ``uploads/{user_id}/`` (the ownership check) and still starting with the upload's ID (its
    journey), and the part blobs derived from it (``part_blob_name``) are the job's own too."""
    directory, _, name = upload_blob_name.rpartition("/")
    upload_id, separator, stem = name[:36], name[36:37], name[37:]
    if separator != "-":  # not an upload's name: nothing to derive it from
        raise ValueError("not the blob name of an upload")
    return f"{directory}/{upload_id}-{job_id}-{stem}"


def source_extension(source_blob: str) -> str:
    """The extension of a source blob's name (the original's, so ffmpeg can sniff its format)."""
    match = re.search(r"(\.[A-Za-z0-9]{1,8})$", source_blob)
    return match.group(1).lower() if match else ""


class AudioStorage(Protocol):
    async def find_source(self, user_id: UUID | str, upload_id: UUID | str) -> str | None:
        """The name of the original stored for this upload, or None if there is none."""

    async def upload(self, path: Path, blob_name: str, *, concurrency: int | None = None) -> bool:
        """Store a file (False if the backend refused it)."""

    async def download(self, blob_name: str, destination: Path) -> None:
        """Copy a blob to a file. FileNotFoundError if there is no such blob."""

    async def delete(self, blob_name: str) -> None:
        """Remove a blob; one that is not there is not an error."""

    async def url(self, blob_name: str) -> str:
        """The URL Azure Speech reads the blob from."""


class LocalAudioStorage:
    """The dev-only local directory (``AUDIO_STORAGE_BACKEND=local``)."""

    async def find_source(self, user_id: UUID | str, upload_id: UUID | str) -> str | None:
        for extension in sorted(AUDIO_EXTENSIONS):
            name = source_blob_name(user_id, upload_id, extension)
            try:
                await asyncio.to_thread(local_storage.size, name)
            except FileNotFoundError:
                continue
            return name
        return None

    async def upload(self, path: Path, blob_name: str, *, concurrency: int | None = None) -> bool:
        await asyncio.to_thread(local_storage.save_file, path, blob_name)
        return True

    async def download(self, blob_name: str, destination: Path) -> None:
        source = await asyncio.to_thread(local_storage.path_of, blob_name)
        if not await asyncio.to_thread(source.exists):
            raise FileNotFoundError(blob_name)
        await asyncio.to_thread(shutil.copyfile, source, destination)

    async def delete(self, blob_name: str) -> None:
        await asyncio.to_thread(local_storage.delete, blob_name)

    async def url(self, blob_name: str) -> str:
        return local_storage.build_url(blob_name)


class AzureAudioStorage:
    """Azure Blob Storage."""

    async def find_source(self, user_id: UUID | str, upload_id: UUID | str) -> str | None:
        prefix = f"{SOURCE_PREFIX}/{user_id}/{upload_id}"
        pattern = re.compile(rf"^{re.escape(prefix)}\.[A-Za-z0-9]{{1,8}}$")
        async with AsyncAzureBlobManager() as manager:
            names = await manager.find_blob_names(prefix)
        return next((name for name in names if pattern.match(name)), None)

    async def upload(self, path: Path, blob_name: str, *, concurrency: int | None = None) -> bool:
        async with AsyncAzureBlobManager() as manager:
            return await manager.create_blob_from_file(path, blob_name, max_concurrency=concurrency)

    async def download(self, blob_name: str, destination: Path) -> None:
        async with AsyncAzureBlobManager() as manager:
            if not await manager.download_blob_to_file(blob_name, destination):
                raise StorageError("The blob could not be downloaded")

    async def delete(self, blob_name: str) -> None:
        async with AsyncAzureBlobManager() as manager:
            await manager.delete_blob(blob_name)

    async def url(self, blob_name: str) -> str:
        async with AsyncAzureBlobManager() as manager:
            return manager.build_blob_url(blob_name)


def get_audio_storage() -> AudioStorage:
    """The storage the settings select."""
    return LocalAudioStorage() if get_settings().AUDIO_STORAGE_BACKEND == "local" else AzureAudioStorage()
