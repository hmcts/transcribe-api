from __future__ import annotations

import math
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from transcription_svc.api.routes import limiter, router
from transcription_svc.config.settings import get_settings

_polling_task = None


def _json_safe(obj: Any) -> Any:
    """Recursively replace non-finite floats (NaN/±Infinity) with their string form.

    FastAPI's default validation-error response echoes the offending input, and
    Starlette serialises responses with allow_nan=False — so a request body
    carrying NaN/Infinity (which Python's JSON parser accepts) would otherwise
    make the 422 itself crash with a 500 while trying to serialise the echoed
    value. Stringifying keeps the error response valid JSON.
    """
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Preserve FastAPI's default {"detail": [...]} response shape — only the
    # non-finite-float sanitisation differs from the built-in handler.
    return JSONResponse(
        status_code=422,
        content={"detail": _json_safe(jsonable_encoder(exc.errors()))},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    from hmcts_azure_auth.roles import validate_approles_config

    from transcription_svc.audio.polling_service import BatchPollingService

    # Fail fast if AUTH_APPROLES is set but contains invalid configuration
    # (bad JSON, missing required role keys, non-string values).
    validate_approles_config()

    settings = get_settings()
    if settings.ENVIRONMENT != "test":
        service = BatchPollingService()
        global _polling_task
        _polling_task = asyncio.create_task(service.run_polling_loop())

    yield

    if _polling_task:
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Batch Audio Transcription Service",
        description=(
            "Submits audio to Azure Batch Speech, polls for completion, "
            "delivers results via webhook."
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.ENVIRONMENT in ("local", "dev") else None,
        redoc_url=None,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(router)
    return app
