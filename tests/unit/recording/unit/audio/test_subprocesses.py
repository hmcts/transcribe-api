"""DIAAT-408: cancelling the task that runs ffmpeg or ffprobe stops the child (stt/subprocesses.py).

Cancelling the awaiting task used to stop only the Python wait: the child went on converting into a
temporary directory the task had already removed. The tests start a long-running child (``sleep 30``,
or a stand-in ffmpeg/ffprobe that execs it), wait until it exists (the spawn is observed, so there is no
sleep and no race), cancel the task, and check by pid that the child is gone and reaped.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys

import pytest

from transcribe_api.stt import splitter
from transcribe_api.stt.preprocessing import normalize_to_wav, probe_audio_duration
from transcribe_api.stt.subprocesses import run_process

WAIT = 20


class Spawns:
    """Every child started through asyncio, and an event for the first one."""

    def __init__(self) -> None:
        self.procs: list[asyncio.subprocess.Process] = []
        self.started = asyncio.Event()


@pytest.fixture
def spawns(monkeypatch):
    seen = Spawns()
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await real(*args, **kwargs)
        seen.procs.append(proc)
        seen.started.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return seen


@pytest.fixture
def slow_tools(tmp_path, monkeypatch):
    """ffmpeg and ffprobe stand-ins that never finish (they exec ``sleep``, so the child IS the sleeper)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\nexec sleep 30\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _is_gone(pid: int) -> bool:
    """No such process: not running, and not a zombie waiting to be reaped either."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def _cancel_once_running(spawns: Spawns, task: asyncio.Task) -> int:
    await asyncio.wait_for(spawns.started.wait(), WAIT)
    (proc,) = spawns.procs
    assert proc.returncode is None and not _is_gone(proc.pid)  # it is running
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, WAIT)
    return proc.pid


async def test_cancelling_run_process_kills_and_reaps_the_child(spawns):
    task = asyncio.create_task(run_process("sleep", "30"))
    pid = await _cancel_once_running(spawns, task)
    assert _is_gone(pid)


async def test_a_timeout_kills_and_reaps_the_child_and_is_raised(spawns):
    with pytest.raises(TimeoutError):
        await run_process(sys.executable, "-c", "import time; time.sleep(30)", timeout=0.2)
    (proc,) = spawns.procs
    assert _is_gone(proc.pid)


async def test_a_child_that_finishes_gives_its_output_and_exit_code():
    result = await run_process(
        sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    )
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (3, b"out", b"err")


async def test_a_program_that_is_not_installed_is_file_not_found():
    with pytest.raises(FileNotFoundError):
        await run_process("ffmpeg-that-does-not-exist-here")


async def test_cancelling_a_probe_stops_ffprobe(spawns, slow_tools, tmp_path):
    task = asyncio.create_task(probe_audio_duration(tmp_path / "in.mp3", timeout=60))
    pid = await _cancel_once_running(spawns, task)
    assert _is_gone(pid)


async def test_cancelling_the_conversion_stops_ffmpeg(spawns, slow_tools, tmp_path):
    task = asyncio.create_task(normalize_to_wav(tmp_path / "in.mp3", tmp_path / "out.wav"))
    pid = await _cancel_once_running(spawns, task)
    assert _is_gone(pid)


async def test_cancelling_silence_detection_stops_ffmpeg(spawns, slow_tools, tmp_path):
    task = asyncio.create_task(splitter.detect_silences(tmp_path / "in.wav", 100.0))
    pid = await _cancel_once_running(spawns, task)
    assert _is_gone(pid)
