"""Cancelling a submission while its hearing document renders leaves no document behind.

The render runs in a worker thread, which cannot be stopped. Without the hand-off in
_render_hearing_document_off_loop the thread would finish after the request was
cancelled and leave the document (transcript and judicial notes) in the temp
directory, including when the service shuts down and its event loop goes away.
"""

import asyncio
import threading
import time
from pathlib import Path

import pytest

from transcribe_api.api import routes_dictation


class _BlockingRender:
    """Stands in for render_hearing_document: blocks until released, then writes."""

    def __init__(self, tmp_path: Path) -> None:
        self.output = tmp_path / "hearing-document.docx"
        self.started = threading.Event()
        self.release = threading.Event()
        self.written = threading.Event()

    def __call__(self, **_kwargs) -> Path:
        self.started.set()
        self.release.wait(timeout=5)
        self.output.write_bytes(b"transcript and judicial notes")
        self.written.set()
        return self.output


@pytest.fixture
def render(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _BlockingRender:
    fake = _BlockingRender(tmp_path)
    monkeypatch.setattr(routes_dictation, "render_hearing_document", fake)
    return fake


def _wait_until_gone(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while path.exists():
        assert time.monotonic() < deadline, f"{path.name} was left behind"
        time.sleep(0.01)


async def _wait_for(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5), "timed out"


@pytest.mark.asyncio
async def test_a_render_cancelled_mid_way_is_deleted_when_the_thread_finishes(render: _BlockingRender) -> None:
    task = asyncio.create_task(routes_dictation._render_hearing_document_off_loop())
    await _wait_for(render.started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    render.release.set()  # the thread now writes the document
    await _wait_for(render.written)
    _wait_until_gone(render.output)


@pytest.mark.asyncio
async def test_a_render_cancelled_after_it_finished_is_deleted(render: _BlockingRender) -> None:
    task = asyncio.create_task(routes_dictation._render_hearing_document_off_loop())
    await _wait_for(render.started)
    render.release.set()
    # Block the loop until the thread has written, so the request is cancelled after
    # the render finished but before the loop handed it the path.
    assert render.written.wait(timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _wait_until_gone(render.output)


def test_a_render_abandoned_at_shutdown_is_deleted_after_the_loop_has_closed(render: _BlockingRender) -> None:
    # Shutdown cancels outstanding tasks and closes the loop; no asyncio callback runs
    # after that, so the worker thread itself must delete the document.
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(routes_dictation._render_hearing_document_off_loop())
        loop.run_until_complete(asyncio.to_thread(render.started.wait, 5))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(task)
    finally:
        loop.close()

    render.release.set()
    assert render.written.wait(timeout=5)
    _wait_until_gone(render.output)


@pytest.mark.asyncio
async def test_a_completed_render_returns_the_document(render: _BlockingRender) -> None:
    render.release.set()

    output = await routes_dictation._render_hearing_document_off_loop()

    assert output.read_bytes() == b"transcript and judicial notes"
