"""Cancelling a submission while its hearing document renders leaves no document behind.

The render runs in a worker thread, which cannot be stopped. Without the cleanup in
_render_hearing_document_off_loop the thread would finish after the request was
cancelled and leave the document (transcript and judicial notes) in the temp directory.
"""

import asyncio
import threading
from pathlib import Path

import pytest

from transcribe_api.api import routes_dictation


def _blocking_render(
    tmp_path: Path, started: threading.Event, release: threading.Event, written: threading.Event | None = None
):
    def render(**_kwargs) -> Path:
        started.set()
        release.wait(timeout=5)
        output = tmp_path / "hearing-document.docx"
        output.write_bytes(b"transcript and judicial notes")
        if written is not None:
            written.set()
        return output

    return render


async def _wait_until(condition, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_cancelled_render_deletes_the_document_once_the_thread_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started, release, written = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(
        routes_dictation, "render_hearing_document", _blocking_render(tmp_path, started, release, written)
    )
    output = tmp_path / "hearing-document.docx"

    task = asyncio.create_task(routes_dictation._render_hearing_document_off_loop())
    await _wait_until(started.is_set)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()  # the thread now writes the document
    await _wait_until(written.is_set)
    await _wait_until(lambda: not output.exists())
    assert not routes_dictation._abandoned_renders


@pytest.mark.asyncio
async def test_a_completed_render_returns_the_document(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started, release = threading.Event(), threading.Event()
    release.set()
    monkeypatch.setattr(routes_dictation, "render_hearing_document", _blocking_render(tmp_path, started, release))

    output = await routes_dictation._render_hearing_document_off_loop()

    assert output.read_bytes() == b"transcript and judicial notes"
    assert not routes_dictation._abandoned_renders
