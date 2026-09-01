"""Merged application factory for the HMCTS Transcribe backend.

Composes the two upstream FastAPI applications into one CNP component:

- dictation routers (product surface, ~40 routes) mounted at root
- recording router (transcription jobs, uploads) mounted at root
- one lifespan, with background pollers OFF in the web process by default

On the pollers: architecture 4.3 requires that batch/queue work does not
contend with request serving, because real-time Speech token minting sits on
the interactive path of a judge dictating. Both upstream services started their
pollers in-process. Here they are opt-in via RUN_WORKERS so the web deployment
serves requests only, and `worker.py` runs them as a separate workload.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from transcribe_api.runtime.settings_dictation import get_settings

log = logging.getLogger("uvicorn")

LOCAL_DEV_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


def _run_workers_in_web_process() -> bool:
    """Whether this process should also run background pollers.

    Default false: the web deployment serves requests only. The worker
    deployment sets RUN_WORKERS=true. See architecture 4.3.
    """
    return os.environ.get("RUN_WORKERS", "").lower() in ("1", "true", "yes")


def _json_safe(obj: Any) -> Any:
    """Replace non-finite floats with their string form.

    Carried over from the recording service: FastAPI echoes offending input in
    422 responses and Starlette serialises with allow_nan=False, so a body
    carrying NaN/Infinity would make the 422 itself crash with a 500.
    """
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": _json_safe(jsonable_encoder(exc.errors()))})


def build_cors_origins(settings: Any) -> list[str]:
    """Derive allowed CORS origins with safe fallbacks (from the dictation service)."""
    from transcribe_api.runtime.cors_utils import parse_origins

    if settings.ENVIRONMENT == "local":
        return LOCAL_DEV_ORIGINS
    origins = parse_origins(settings.CORS_ALLOWED_ORIGINS)
    if origins:
        return origins
    app_url_origins = parse_origins(settings.APP_URL)
    if app_url_origins:
        log.warning(
            "CORS_ALLOWED_ORIGINS not configured for %s; falling back to APP_URL",
            settings.ENVIRONMENT,
        )
        return app_url_origins
    log.warning("CORS configuration missing for %s; defaulting to local dev origins.", settings.ENVIRONMENT)
    return LOCAL_DEV_ORIGINS


async def start_background_workers() -> list[asyncio.Task]:
    """Start both pollers. Used by the worker entrypoint and, optionally, the web process."""
    from transcribe_api.stt.polling_service import BatchPollingService
    from transcribe_api.stt.work_poller import TranscriptionPollingService

    tasks = [
        asyncio.create_task(BatchPollingService().run_polling_loop()),
        asyncio.create_task(TranscriptionPollingService().run_polling_loop()),
    ]
    log.info("Started %d background poller(s)", len(tasks))
    return tasks


async def stop_background_workers(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    if tasks:
        log.info("Background pollers stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from hmcts_azure_auth.roles import validate_approles_config

    settings = get_settings()

    # Fail fast on bad AUTH_APPROLES. Both upstream services did this; the
    # dictation service skipped it locally so developers are not blocked.
    if settings.ENVIRONMENT not in ("local", "test"):
        validate_approles_config()

    tasks: list[asyncio.Task] = []
    if settings.ENVIRONMENT != "test" and _run_workers_in_web_process():
        tasks = await start_background_workers()

    yield

    await stop_background_workers(tasks)


def create_app() -> FastAPI:
    settings = get_settings()

    if settings.SENTRY_DSN:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENVIRONMENT,
            send_default_pii=False,
            traces_sample_rate=1.0,
            profile_session_sample_rate=1.0,
            profile_lifecycle="trace",
        )

    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor()

    from transcribe_api.runtime.version import APP_VERSION

    app = FastAPI(
        title="HMCTS Transcribe API",
        description="Transcription (recording and dictation), document generation and judicial domain.",
        version=APP_VERSION,
        lifespan=lifespan,
        openapi_url="/api/openapi.json",
        docs_url="/docs" if settings.ENVIRONMENT in ("local", "dev") else None,
        redoc_url=None,
    )

    if not getattr(settings, "DISABLE_FASTAPI_INSTRUMENTATION", False):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)

    # --- middleware ---
    origins = build_cors_origins(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    from transcribe_api.runtime.middleware import add_request_id

    app.middleware("http")(add_request_id)

    # --- exception handlers ---
    from transcribe_api.runtime.exception_handlers import (
        http_exception_handler,
        unhandled_exception_handler,
    )

    # MERGE NOTE: these two handlers take (exc) only, not (request, exc). The
    # dictation app wrapped them so it could record the exception on the current
    # OpenTelemetry span before delegating. Registering them bare gives
    # "takes 1 positional argument but 2 were given" on every error path.
    from opentelemetry import trace

    async def _http_exception_wrapper(request: Request, exc: FastAPIHTTPException):
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(trace.StatusCode.ERROR, str(exc))
        return await http_exception_handler(exc)

    async def _unhandled_exception_wrapper(request: Request, exc: Exception):
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(trace.StatusCode.ERROR, str(exc))
        return await unhandled_exception_handler(exc)

    app.add_exception_handler(FastAPIHTTPException, _http_exception_wrapper)
    app.add_exception_handler(Exception, _unhandled_exception_wrapper)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)

    # --- routers ---
    from transcribe_api.api.routes_dictation import router as dictation_router
    from transcribe_api.api.routes_dictation_privileged import router as dictation_admin_router
    from transcribe_api.api.routes_recording import limiter
    from transcribe_api.api.routes_recording import router as recording_router

    # Prefixes matter and differ between the two upstream apps:
    #   recording router declares its own prefix="/api/v1"  -> mount bare
    #   dictation router has no prefix, upstream mounted at prefix="/api"
    #   dictation admin declares prefix="/admin", also under "/api"
    # So the surfaces are /api/v1/* and /api/* (+ /api/admin/*) and do NOT
    # collide — including /health, which exists in both but at different paths.
    app.include_router(dictation_router, prefix="/api")
    app.include_router(dictation_admin_router, prefix="/api")
    app.include_router(recording_router)

    # slowapi rate limiting (recording service)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    return app
