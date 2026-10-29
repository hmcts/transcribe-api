"""Browser telemetry relay for transcribe-web (OBS-4: DIAAT-278 and DIAAT-300; OBS-3).

The browser posts batches to ``POST /api/telemetry`` (same origin: Caddy forwards
``/api/*`` here with the session's bearer token). Only events registered as browser
events in the merged schema (runtime/telemetry_schema.py) are accepted, each with its
own attribute list, and every string attribute is an enumeration or a pattern: no free
text, transcript, notes or case metadata can be relayed. Everything else is dropped and
counted.

Operational events (whether dictation works) are always sent. Product analytics
(OBS-3, replacing PostHog) are sent only with the cookie banner's analytics consent,
checked in the browser and again here.
"""

from __future__ import annotations

import json
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from hmcts_otel.fastapi import create_telemetry_relay_router

import transcribe_api.runtime.telemetry  # noqa: F401  (registers the merged schema)

# One journey per browser dictation session, with a browser-generated UUID.
JOURNEY_DICTATION_SESSION = "dictation.session"

# transcribe-web's cookie banner (app/cookies/page.tsx, @hmcts/cookie-manager format).
CONSENT_COOKIE = "judicial-transcribe-cookie-preferences"


def analytics_consent(request: Request) -> bool:
    """The cookie banner's analytics preference (``{"analytics": "on"}``, as set by
    @hmcts/cookie-manager or the cookie settings page, possibly URL-encoded)."""
    raw = request.cookies.get(CONSENT_COOKIE)
    if not raw:
        return False
    for candidate in (raw, unquote(raw)):
        try:
            preferences = json.loads(candidate)
        except ValueError:
            continue
        return isinstance(preferences, dict) and preferences.get("analytics") == "on"
    return False


def _rate_limit_by_user(request: Request) -> str | None:
    """Rate-limit per signed-in user: behind the ingress every request has the same address."""
    return getattr(request.state, "telemetry_identity", None)


def telemetry_relay_router(authenticated_user_dependency) -> APIRouter:
    """``POST /telemetry`` for the browser, behind the app's authentication."""

    async def remember_identity(request: Request, user=Depends(authenticated_user_dependency)) -> None:
        request.state.telemetry_identity = str(user.id)

    return create_telemetry_relay_router(
        dependencies=[Depends(remember_identity)],
        identity=_rate_limit_by_user,
        analytics_consent=analytics_consent,
        # Journey IDs come from the client: only the browser's own session journey,
        # never a server-side one (e.g. another user's transcription).
        journey_types={JOURNEY_DICTATION_SESSION},
        # A session sends a batch every few seconds at most; this leaves room for
        # retries after an outage without letting one client flood the pipeline.
        rate_limit_per_minute=60,
    )
