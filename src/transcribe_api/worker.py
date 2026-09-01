"""Worker entrypoint — background pollers only, no request serving.

Deployed as a separate workload from the API (architecture 4.3) so that batch
transcription backlogs cannot degrade real-time Speech token minting.

    python -m transcribe_api.worker
"""

from __future__ import annotations

import asyncio
import logging

from transcribe_api.api.app import start_background_workers, stop_background_workers

log = logging.getLogger("worker")


async def main() -> None:
    tasks = await start_background_workers()
    log.info("Worker running; press Ctrl-C to stop")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        await stop_background_workers(tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Worker stopped")
