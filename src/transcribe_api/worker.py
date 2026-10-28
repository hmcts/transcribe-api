"""Worker entrypoint — background pollers only, no request serving.

Deployed as a separate workload from the API (architecture 4.3) so that batch
transcription backlogs cannot degrade real-time Speech token minting.

    python -m transcribe_api.worker
"""

from __future__ import annotations

import asyncio
import logging
import signal

from hmcts_otel import shutdown_telemetry

from transcribe_api.api.app import (
    ensure_ffmpeg_tools,
    start_background_workers,
    stop_background_workers,
)
from transcribe_api.runtime.settings_dictation import get_settings
from transcribe_api.runtime.telemetry import SERVICE_NAME_WORKER, configure_telemetry

log = logging.getLogger("worker")


async def main() -> None:
    # The pollers convert and probe audio with ffmpeg/ffprobe.
    await ensure_ffmpeg_tools(get_settings().ENVIRONMENT)
    tasks = await start_background_workers()
    # Kubernetes stops the pod with SIGTERM, whose default exits at once, skipping the
    # finally below and the telemetry flush. Cancel instead, so shutdown runs as for Ctrl-C.
    main_task = asyncio.current_task()
    if main_task is not None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)
    log.info("Worker running; press Ctrl-C to stop")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        await stop_background_workers(tasks)


if __name__ == "__main__":
    configure_telemetry(SERVICE_NAME_WORKER)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Worker stopped")
    finally:
        shutdown_telemetry()
