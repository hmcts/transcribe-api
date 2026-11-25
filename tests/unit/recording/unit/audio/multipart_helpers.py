"""Shared setup of the DIAAT-408 tests of recordings in parts: the scaled-down limits (Azure's 240 minutes
becomes 30 seconds, the part target 22 seconds), a deferred job with its original in storage, and
preparing it so that its parts are submitted to the mock Speech backend."""

from __future__ import annotations

import itertools
from uuid import uuid4

from tests.unit.recording.blob_stores import (
    MemoryBlobStorage,
    install_memory_blob_storage,
    installed_memory_blob_storage,
)
from tests.unit.recording.unit.async_upload_helpers import USER_ID, add_job, tone_and_silence
from transcribe_api.runtime import settings_dictation, settings_recording
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.stt import part_submission, polling_service, recording_limits
from transcribe_api.stt import preparation_service as prep
from transcribe_api.stt.preparation_service import PreparationService

LIMIT = 30.0
TARGET = 22.0
THREE_PARTS = (("t", 20.0), ("s", 2.0), ("t", 20.0), ("s", 2.0), ("t", 20.0))  # 64 s
FIVE_PARTS = tuple(itertools.chain.from_iterable((("t", 20.0), ("s", 2.0)) for _ in range(5)))  # 110 s


def scale_down(monkeypatch, mocker) -> MemoryBlobStorage:
    """In-memory storage, the mock Speech backend, both flags on, and the limits at test scale."""
    storage = install_memory_blob_storage(monkeypatch)
    monkeypatch.setenv("SPEECH_BACKEND", "mock")
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", "true")
    monkeypatch.setenv("LONG_RECORDINGS_ENABLED", "true")
    settings_recording.get_settings.cache_clear()
    settings_dictation.get_settings.cache_clear()
    settings = get_settings()
    mocker.patch.object(settings, "MAX_AUDIO_DURATION_SECONDS", 1000)
    mocker.patch.object(settings, "PART_TARGET_SECONDS", TARGET)
    mocker.patch.object(settings, "PART_RETRY_BACKOFF_SECONDS", 0)
    mocker.patch.object(settings, "PREPARE_RETRY_BACKOFF_SECONDS", 0)
    mocker.patch.object(prep, "AZURE_BATCH_FILE_LIMIT_SECONDS", LIMIT)
    mocker.patch.object(recording_limits, "AZURE_BATCH_FILE_LIMIT_SECONDS", LIMIT)
    return storage


def use_engine(mocker, engine) -> None:
    for module in (prep, part_submission, polling_service):
        mocker.patch.object(module, "get_engine", new=lambda: engine)


def waiting(engine, pattern=THREE_PARTS, **fields):
    """A deferred job whose original (a WAV: the service converts it anyway) is in storage."""
    upload_id = uuid4()
    job = add_job(engine, upload_id=upload_id, source_blob_path=f"incoming/{USER_ID}/{upload_id}.wav", **fields)
    installed_memory_blob_storage().put(job.source_blob_path, tone_and_silence(pattern))
    return job


async def prepare(service: PreparationService | None = None) -> PreparationService:
    service = service or PreparationService()
    await service.poll_once()
    await service.wait_idle()
    return service
