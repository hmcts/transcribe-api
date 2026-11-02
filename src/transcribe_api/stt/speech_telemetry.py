"""Azure Speech as an external dependency (DIAAT-282).

* Call metrics: every HTTP call to Azure Speech is timed and counted by operation,
  status and outcome (``transcription.speech.call.duration``), failures are counted by
  code (``transcription.speech.call.failures``) and throttled (429) responses on their
  own (``transcription.speech.throttled``). Metrics are not sampled, unlike the httpx
  dependency spans of the Azure Monitor distro.
* Configuration reference: the region, locale, API version, model and diarization a
  job was sent with, for job.queued and job.finished.
* Concurrency: the running-jobs gauge is Azure Speech's concurrency. When
  ``SPEECH_CONCURRENT_JOBS_LIMIT`` is set, the utilisation gauge relates it to that limit.

Telemetry only: nothing here changes how calls are made, retried or throttled.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

import httpx
from hmcts_otel import conventions as c

from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import (
    ATTR_HTTP_STATUS,
    ATTR_SPEECH_API_VERSION,
    ATTR_SPEECH_DIARIZATION,
    ATTR_SPEECH_LOCALE,
    ATTR_SPEECH_MODEL_REF,
    ATTR_SPEECH_REGION,
)
from transcribe_api.stt.job_telemetry import (
    HTTP_TOO_MANY_REQUESTS,
    _instrument,
    failure_code,
)

logger = logging.getLogger(__name__)

# Speech to text REST API version used for every call (the submit URL's path).
SPEECH_API_VERSION = "v3.2"

METRIC_SPEECH_CALL_DURATION = "transcription.speech.call.duration"
METRIC_SPEECH_CALL_FAILURES = "transcription.speech.call.failures"
METRIC_SPEECH_THROTTLED = "transcription.speech.throttled"

# Metric attribute: the Azure Speech call. The first four are job.retry.operation's
# values; speech.model is the un-retried model lookup.
ATTR_SPEECH_OPERATION = "speech.operation"
SPEECH_OPERATIONS = (
    "speech.submit",
    "speech.status",
    "speech.results",
    "speech.delete",
    "speech.model",
)

_HTTP_CLIENT_ERROR_MIN = 400
_HTTP_SERVER_ERROR_MIN = 500


# Call metrics -----------------------------------------------------------------------------


class SpeechCall:
    """One HTTP call to Azure Speech. ``observe()`` the response as soon as it arrives."""

    def __init__(self, operation: str, ok_statuses: Collection[int] = ()) -> None:
        self.operation = operation
        self._ok_statuses = ok_statuses
        self._response: httpx.Response | None = None

    def observe(self, response: httpx.Response) -> httpx.Response:
        self._response = response
        return response

    def attributes(self, exc: BaseException | None) -> dict[str, Any]:
        """Metric attributes: operation, status, outcome and a controlled code."""
        attributes: dict[str, Any] = {ATTR_SPEECH_OPERATION: self.operation}
        if self._response is not None:
            status = int(self._response.status_code)
            attributes[ATTR_HTTP_STATUS] = status
            if status < _HTTP_CLIENT_ERROR_MIN or status in self._ok_statuses:
                outcome, code = "success", "none"
            elif status == HTTP_TOO_MANY_REQUESTS:
                outcome, code = "failure", "speech.throttled"
            elif status < _HTTP_SERVER_ERROR_MIN:
                outcome, code = "failure", f"speech.http_{status}"
            else:
                outcome, code = "error", f"speech.http_{status}"
        elif exc is not None:
            outcome, code = "error", failure_code(exc, "speech")
        else:
            outcome, code = "success", "none"
        attributes[c.OUTCOME] = outcome
        attributes[c.ERROR_CODE] = code
        return attributes


def _record_call(call: SpeechCall, seconds: float, exc: BaseException | None) -> None:
    attributes = call.attributes(exc)
    _instrument(
        "histogram",
        METRIC_SPEECH_CALL_DURATION,
        unit="s",
        description="Duration of HTTP calls to Azure Speech",
    ).record(seconds, attributes)
    if attributes[c.OUTCOME] == "success":
        return
    _instrument(
        "counter",
        METRIC_SPEECH_CALL_FAILURES,
        unit="{call}",
        description="Failed HTTP calls to Azure Speech",
    ).add(1, {ATTR_SPEECH_OPERATION: call.operation, c.ERROR_CODE: attributes[c.ERROR_CODE]})
    if attributes[c.ERROR_CODE] == "speech.throttled":
        _instrument(
            "counter",
            METRIC_SPEECH_THROTTLED,
            unit="{call}",
            description="Azure Speech calls throttled (HTTP 429): the approaching-limit signal",
        ).add(1, {ATTR_SPEECH_OPERATION: call.operation})


@contextmanager
def speech_call(operation: str, *, ok_statuses: Collection[int] = ()) -> Iterator[SpeechCall]:
    """Time and count an Azure Speech HTTP call. Never fails or alters the call.

    ``ok_statuses``: statuses the caller treats as success (404 on delete).
    """
    call = SpeechCall(operation, ok_statuses)
    started = time.perf_counter()
    try:
        yield call
    except Exception as exc:  # cancellation is not a Speech failure: not recorded
        _safely_record(call, time.perf_counter() - started, exc)
        raise
    _safely_record(call, time.perf_counter() - started, None)


def _safely_record(call: SpeechCall, seconds: float, exc: BaseException | None) -> None:
    try:
        _record_call(call, seconds, exc)
    except Exception as error:  # telemetry must never break the call
        logger.warning("Could not record Azure Speech call (%s)", type(error).__name__)


# Configuration reference -----------------------------------------------------------------

_REGION = re.compile(r"[a-z0-9]{2,32}")
_REGIONAL_HOST = re.compile(r"([a-z0-9]{2,32})\.(?:api\.cognitive\.microsoft\.com|stt\.speech\.microsoft\.com)")
_LOCALE = re.compile(r"[a-z]{2,3}-[A-Z]{2}")
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def speech_region() -> str | None:
    """AZURE_SPEECH_REGION, else the region in a regional endpoint's host; else None.

    A custom-subdomain endpoint (<name>.cognitiveservices.azure.com, as deployed) does
    not name its region.
    """
    settings = get_settings()
    configured = (settings.AZURE_SPEECH_REGION or "").strip().lower()
    if _REGION.fullmatch(configured):
        return configured
    try:
        host = (urlparse(settings.AZURE_SPEECH_ENDPOINT).hostname or "").lower()
    except ValueError:
        return None
    match = _REGIONAL_HOST.fullmatch(host)
    return match.group(1) if match else None


def model_ref(status_data: Mapping[str, Any]) -> str | None:
    """The GUID of the model Azure used (the last segment of ``model.self``), never the URL."""
    model = status_data.get("model")
    model_self = model.get("self") if isinstance(model, Mapping) else None
    if not isinstance(model_self, str):
        return None
    guid = model_self.rstrip("/").rsplit("/", 1)[-1]
    return guid if _GUID.fullmatch(guid) else None


def speech_config_attributes(
    locale: str | None, diarization: bool | None, status_data: Mapping[str, Any] | None = None
) -> dict[str, object]:
    """Region, locale, API version, model and diarization of a job: what is known."""
    attributes: dict[str, object] = {ATTR_SPEECH_API_VERSION: SPEECH_API_VERSION}
    try:
        region = speech_region()
        if region is not None:
            attributes[ATTR_SPEECH_REGION] = region
        if isinstance(locale, str) and _LOCALE.fullmatch(locale):
            attributes[ATTR_SPEECH_LOCALE] = locale
        if isinstance(diarization, bool):
            attributes[ATTR_SPEECH_DIARIZATION] = diarization
        model = model_ref(status_data or {})
        if model is not None:
            attributes[ATTR_SPEECH_MODEL_REF] = model
    except Exception as exc:
        logger.warning("Could not read the Speech configuration (%s)", type(exc).__name__)
    return attributes
