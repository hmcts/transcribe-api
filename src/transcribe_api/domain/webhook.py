from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


def _safe_url(url: str) -> str:
    """Return scheme+host only — strips path and query params from logs.

    Prevents SAS tokens or other sensitive query parameters from appearing
    in log aggregators, and avoids leaking internal service topology.
    """
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


_DEFAULT_RETRIES = 3
_DEFAULT_TIMEOUT = 30.0


async def dispatch(
    callback_url: str,
    webhook_secret: str,
    payload: dict,
    *,
    max_retries: int = _DEFAULT_RETRIES,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bool:
    """POST a signed webhook payload to callback_url.

    Returns True if the delivery was confirmed (2xx response).
    4xx responses stop retrying but return False — the receiver rejected
    the payload and retrying won't help.
    Retries up to max_retries times on network errors or 5xx responses
    with exponential backoff (1s, 2s, 4s).
    """
    body = json.dumps(payload, default=str)
    signature = _sign(body, webhook_secret)
    headers = {
        "Content-Type": "application/json",
        "X-Signature-256": f"sha256={signature}",
        "User-Agent": "transcription-svc/1.0",
    }

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(callback_url, content=body, headers=headers)
                if response.status_code < 500:
                    logger.info(
                        "Webhook delivered to %s (status=%d)",
                        _safe_url(callback_url),
                        response.status_code,
                    )
                    return response.status_code < 300
                logger.warning(
                    "Webhook attempt %d/%d got %d from %s",
                    attempt + 1,
                    max_retries,
                    response.status_code,
                    _safe_url(callback_url),
                )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning(
                "Webhook attempt %d/%d failed (%s): %s",
                attempt + 1,
                max_retries,
                type(exc).__name__,
                _safe_url(callback_url),
            )

        if attempt < max_retries - 1:
            await asyncio.sleep(2**attempt)

    logger.error(
        "Webhook delivery failed after %d attempts: %s", max_retries, _safe_url(callback_url)
    )
    return False


def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def verify_signature(body: str, secret: str, signature_header: str) -> bool:
    """Verify an incoming X-Signature-256 header value.

    Use this in webhook receiver endpoints to authenticate the payload.
    """
    expected = f"sha256={_sign(body, secret)}"
    return hmac.compare_digest(expected, signature_header)
