"""Authentication for the Azure Speech REST APIs (batch and fast transcription).

Deployed pods reach Speech with their workload identity: the Speech account
has key auth disabled and no key is mounted, so each request carries an Entra
bearer token for the Cognitive Services scope. The REST calls must go to the
account's custom-subdomain endpoint (AZURE_SPEECH_ENDPOINT); the regional
endpoints do not accept Entra tokens. A subscription key is used instead only
when AZURE_SPEECH_KEY is set, which is for local development.

Live dictation mints its browser token the same way (api/routes_dictation.py,
get_speech_token).
"""

from __future__ import annotations

import asyncio
from functools import cache

from azure.identity import DefaultAzureCredential

SPEECH_SCOPE = "https://cognitiveservices.azure.com/.default"


class SpeechAuthError(Exception):
    """No credential for Azure Speech could be obtained."""


@cache
def _credential() -> DefaultAzureCredential:
    # One credential for the process; it caches the token and refreshes it
    # before expiry. The synchronous credential is used (via a thread) because
    # it is not tied to an event loop, unlike azure.identity.aio.
    return DefaultAzureCredential()


async def speech_auth_headers(api_key: str) -> dict[str, str]:
    """Return the auth header for a Speech REST request.

    Raises:
        SpeechAuthError: if no key is set and no Entra token can be obtained.
    """
    if api_key:
        return {"Ocp-Apim-Subscription-Key": api_key}
    try:
        token = await asyncio.to_thread(_credential().get_token, SPEECH_SCOPE)
    except Exception as exc:
        raise SpeechAuthError(
            f"Could not get an Entra token for Azure Speech from the workload identity: {exc}"
        ) from exc
    return {"Authorization": f"Bearer {token.token}"}
