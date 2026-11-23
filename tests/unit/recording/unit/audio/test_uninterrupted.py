"""DIAAT-408: a blocking write that a cancelled task started is waited for (stt/uninterrupted.py).

``await asyncio.to_thread(f)`` stops waiting when the task is cancelled but the thread goes on: it can finish a
copy, or recreate a blob the caller's cleanup just deleted, after the task has let go of its lease and removed its
temporary directory. Deterministic, no sleeps: the thread is held on an event; the task is cancelled twice and
seen still waiting; only when the thread is released does the task end, cancelled, with the thread finished.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from transcribe_api.runtime import blob_recording
from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager
from transcribe_api.stt import audio_storage, local_storage
from transcribe_api.stt.audio_storage import LocalAudioStorage
from transcribe_api.stt.uninterrupted import run_to_completion

WAIT = 20


class Held:
    """A blocking call that signals when it has started and finishes only when released."""

    def __init__(self) -> None:
        self.started, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()

    def block(self) -> None:
        self.started.set()
        assert self.release.wait(WAIT)

    async def cancel_twice_and_finish(self, task: asyncio.Task) -> None:
        """The task is cancelled twice while the call is held: it must not be done until the call finishes, and
        then it ends cancelled."""
        assert await asyncio.to_thread(self.started.wait, WAIT)
        task.cancel()
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()  # still waiting for the thread
        assert not self.finished.is_set()
        self.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT)
        assert self.finished.is_set()  # the call had ended when the cancellation was raised


async def test_a_cancelled_call_is_waited_for_through_repeated_cancellations():
    held = Held()

    def call():
        try:
            held.block()
        finally:
            held.finished.set()

    task = asyncio.create_task(run_to_completion(call))
    await held.cancel_twice_and_finish(task)


async def test_the_result_and_the_errors_of_a_call_that_is_not_cancelled_are_unchanged():
    assert await run_to_completion(lambda a, b=0: a + b, 1, b=2) == 3
    with pytest.raises(ZeroDivisionError):
        await run_to_completion(lambda: 1 / 0)


async def test_an_error_of_a_call_that_was_cancelled_does_not_replace_the_cancellation():
    held = Held()

    def call():
        try:
            held.block()
            raise OSError("failed after being cancelled")
        finally:
            held.finished.set()

    task = asyncio.create_task(run_to_completion(call))
    await held.cancel_twice_and_finish(task)  # CancelledError, not the OSError


async def test_a_local_upload_is_not_abandoned_by_a_cancellation(monkeypatch, tmp_path):
    held = Held()

    def save_file(path, blob_name):
        try:
            held.block()
        finally:
            held.finished.set()

    monkeypatch.setattr(local_storage, "save_file", save_file)
    task = asyncio.create_task(LocalAudioStorage().upload(tmp_path / "x", "uploads/u/a.wav"))
    await held.cancel_twice_and_finish(task)


async def test_a_local_download_copy_is_not_abandoned_by_a_cancellation(monkeypatch, tmp_path):
    held = Held()
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    monkeypatch.setattr(local_storage, "path_of", lambda blob_name: source)

    def copyfile(src, dst):
        try:
            held.block()
        finally:
            held.finished.set()

    monkeypatch.setattr(audio_storage.shutil, "copyfile", copyfile)
    task = asyncio.create_task(LocalAudioStorage().download("uploads/u/a.wav", tmp_path / "out.bin"))
    await held.cancel_twice_and_finish(task)


async def test_a_local_delete_is_not_abandoned_by_a_cancellation(monkeypatch):
    held = Held()

    def delete(blob_name):
        try:
            held.block()
        finally:
            held.finished.set()

    monkeypatch.setattr(local_storage, "delete", delete)
    task = asyncio.create_task(LocalAudioStorage().delete("uploads/u/a.wav"))
    await held.cancel_twice_and_finish(task)


class _Downloader:
    def __init__(self, chunks):
        self._chunks = chunks

    async def chunks(self):
        for chunk in self._chunks:
            yield chunk


class _Blob:
    async def exists(self):
        return True

    async def download_blob(self):
        return _Downloader([b"abc", b"def"])


class _Service:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get_blob_client(self, container, blob):
        return _Blob()


async def test_a_blob_download_write_is_not_abandoned_by_a_cancellation(monkeypatch, tmp_path):
    held = Held()
    target = tmp_path / "out.bin"
    real_open = open

    class SlowFile:
        def __init__(self):
            self.file = real_open(target, "wb")
            self.writes = 0

        def write(self, chunk):
            self.writes += 1
            if self.writes == 1:
                try:
                    held.block()  # the first chunk's write is held
                finally:
                    held.finished.set()
            return self.file.write(chunk)

        def close(self):
            self.file.close()

    monkeypatch.setattr(blob_recording, "open", lambda *a, **k: SlowFile(), raising=False)
    manager = AsyncAzureBlobManager()
    monkeypatch.setattr(manager, "_blob_service_client", lambda **kw: _Service())
    task = asyncio.create_task(manager.download_blob_to_file("incoming/u/a.mp3", target))
    await held.cancel_twice_and_finish(task)
    assert (
        target.read_bytes() == b"abc"
    )  # the write that was in flight finished, and the file was closed; nothing after it


async def test_a_blob_download_is_still_written_whole_without_a_cancellation(monkeypatch, tmp_path):
    manager = AsyncAzureBlobManager()
    monkeypatch.setattr(manager, "_blob_service_client", lambda **kw: _Service())
    target = tmp_path / "out.bin"
    assert await manager.download_blob_to_file("incoming/u/a.mp3", target) is True
    assert Path(target).read_bytes() == b"abcdef"
