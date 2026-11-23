"""Running ffmpeg and ffprobe so that nothing is left running behind a task that stops (DIAAT-408).

``asyncio.Process.communicate()`` does not stop the child when the task awaiting it is cancelled (a
worker shutting down, a deleted job, a request that went away) or times out: the Python wait ends and
the child goes on, writing into a temporary directory the task has already removed, for as long as an
8 hour recording takes to convert. ``run_process`` is the one place that starts such a child, and on
ANY way out other than the child finishing (an exception of any kind, ``asyncio.CancelledError``
included) it kills the child and waits for it to be gone before it re-raises.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

# SIGKILL ends a process at once; this only bounds the wait for a child stuck in an uninterruptible
# state (a hung disk), so that stopping a task never hangs with it.
REAP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


async def run_process(*command: str, timeout: float | None = None) -> ProcessResult:
    """Run ``command``, collecting its output.

    Raises ``FileNotFoundError`` if the program is not installed and ``TimeoutError`` if it is still
    running after ``timeout`` seconds. If the call is left for any reason before the child has finished
    (that timeout, a cancellation, any other exception) the child is killed and reaped first, and the
    exception is raised again: when this returns or raises, the child is not running."""
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException as exc:
        cancelled_again = await _kill_and_reap(proc)
        if cancelled_again and not isinstance(exc, asyncio.CancelledError):
            raise asyncio.CancelledError from exc  # a cancellation that arrived while stopping the child
        raise
    return ProcessResult(proc.returncode if proc.returncode is not None else -1, stdout, stderr)


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> bool:
    """Kill the child and wait (at most ``REAP_TIMEOUT_SECONDS``) until it has exited. The wait is
    shielded: a second cancellation does not abandon the reaping, it is reported to the caller.
    Returns whether this task was cancelled meanwhile."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    reaping = asyncio.ensure_future(asyncio.wait_for(proc.wait(), REAP_TIMEOUT_SECONDS))
    reaping.add_done_callback(lambda task: task.cancelled() or task.exception())  # never "not retrieved"
    try:
        await asyncio.shield(reaping)
    except asyncio.CancelledError:
        return True
    except Exception:  # the child would not exit in time: nothing more can be done for it here
        return False
    return False
