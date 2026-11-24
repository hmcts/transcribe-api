"""Running a blocking call in a thread so that cancelling the task cannot leave it running (DIAAT-408).

``await asyncio.to_thread(f)`` stops WAITING when the task is cancelled, but the thread goes on: a file
copy can finish, or recreate a blob the caller's cleanup has just deleted, after the task has let go of
its lease and removed its temporary directory. ``run_to_completion`` waits for the thread to finish, through
any number of cancellations, and only then raises the cancellation.

Use it for the calls that WRITE into a job's temporary directory or into storage. A call that only reads,
or that is a short database transaction, does not need it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def wait_until_done(work: asyncio.Future) -> None:
    """Return once ``work`` has finished. A cancellation of this task while it waits is not acted on until
    ``work`` has finished: the wait carries on first (a further cancellation nests another wait), and the
    cancellation is then raised, as it must be."""
    while not work.done():
        try:
            await asyncio.wait({work})
        except asyncio.CancelledError:
            await wait_until_done(work)
            raise


async def run_to_completion[T](func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """``await asyncio.to_thread(func, ...)``, but a cancellation of the task is raised only after the thread
    has finished (the result, or the error, of a call that was cancelled is discarded)."""
    work = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await wait_until_done(work)
        if not work.cancelled():
            work.exception()  # marked retrieved: its own outcome is not wanted
        raise
