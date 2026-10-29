"""Dependency health (DIAAT-277): is each critical dependency working?

A background checker in the API process probes each dependency on an interval and
caches the result. Nothing here runs on a request: ``GET /api/health/dependencies``
and the gauge read the cache. One checker for both areas (dictation and recording):
they share the database, the storage account and the Speech resource.

Probes, the cheapest read-only call for each:

* ``database``: ``SELECT 1`` on the application's own engine (so pool exhaustion shows).
* ``storage``: the properties of both containers, ``audio`` and ``transcriptions``
  (Azure Blob Storage, managed identity). One small request each, no listing.
* ``speech``: ``GET .../speechtotext/<version>/models/base?top=1`` with the pod's
  workload identity, the credential batch and fast transcription use. A read of the
  service catalogue: no audio, nothing billed. At most once per
  ``SPEECH_MIN_INTERVAL_S`` (300 s). Not checked with the mock Speech backend.

There is no ``llm`` check: minutes use providers through litellm and none has a cheap
read-only call; LLM failures show in the ``minutes_generation`` stage outcome instead.

Telemetry:

* ``service.dependency.up`` gauge: 1 up, 0 down, per ``dependency.name``; no
  observation until a dependency has been checked once;
* ``service.dependency.check.duration`` histogram (s), by ``dependency.name`` and
  ``hmcts.outcome`` (success, or error for a failed or timed-out check);
* ``dependency.health_changed`` event when a dependency goes up -> down or down -> up
  (or is down on its first check; up on the first check is not a change), with
  ``dependency.state`` and, when down, a controlled ``hmcts.error.code``.

Only the exception's type is logged: messages can carry hostnames, URLs or keys.
Each replica checks independently: aggregate the gauge with min (any replica down)
or average, never sum.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from hmcts_otel import Outcome, get_meter, record_event
from hmcts_otel import conventions as c
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.metrics import CallbackOptions, Meter, Observation
from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import (
    ATTR_DEPENDENCY_NAME,
    ATTR_DEPENDENCY_STATE,
    EVENT_DEPENDENCY_HEALTH_CHANGED,
)

logger = logging.getLogger(__name__)

METRIC_DEPENDENCY_UP = "service.dependency.up"
METRIC_DEPENDENCY_CHECK_DURATION = "service.dependency.check.duration"

DATABASE = "database"
STORAGE = "storage"
SPEECH = "speech"

SPEECH_MIN_INTERVAL_S = 300.0

CODE_TIMEOUT = "dependency.timeout"
CODE_AUTH_FAILED = "dependency.auth_failed"
CODE_UNREACHABLE = "dependency.unreachable"
CODE_ERROR = "dependency.error"

STATUS_UP = "up"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"

_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
# PostgreSQL SQLSTATE class 28: invalid authorization specification.
_PG_AUTH_CLASS = "28"


def error_code(exc: BaseException) -> str:
    """A controlled code for a failed check. Never the message."""
    from transcribe_api.stt.speech_auth import SpeechAuthError

    if isinstance(
        exc,
        (
            TimeoutError,
            httpx.TimeoutException,
            ServiceRequestTimeoutError,
            ServiceResponseTimeoutError,
            sa_exc.TimeoutError,  # no connection free in the pool
        ),
    ):
        return CODE_TIMEOUT
    if isinstance(exc, (ClientAuthenticationError, SpeechAuthError)):
        return CODE_AUTH_FAILED
    status = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    elif isinstance(exc, HttpResponseError):
        status = exc.status_code
    if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
        return CODE_AUTH_FAILED
    if isinstance(exc, sa_exc.DBAPIError):
        sqlstate = getattr(exc.orig, "pgcode", None)
        if isinstance(sqlstate, str) and sqlstate.startswith(_PG_AUTH_CLASS):
            return CODE_AUTH_FAILED
        if _is_database_auth_message(exc.orig):
            return CODE_AUTH_FAILED
        if isinstance(exc, (sa_exc.OperationalError, sa_exc.InterfaceError)):
            return CODE_UNREACHABLE
    if isinstance(exc, (httpx.TransportError, ServiceRequestError, ServiceResponseError, OSError)):
        return CODE_UNREACHABLE
    return CODE_ERROR


def _is_database_auth_message(orig: object) -> bool:
    """libpq reports connection-time auth failures without a SQLSTATE. The message is
    read to classify only; it is never logged or exported."""
    text_ = str(orig or "")
    return "password authentication failed" in text_ or "no pg_hba.conf entry" in text_


@dataclass(frozen=True)
class DependencyCheck:
    """One dependency probe: raises if the dependency is not working.

    ``probe`` is a coroutine function, or a blocking function (``blocking=True``) run
    on the checker's own threads. ``min_interval_s``: probe at most this often.
    """

    name: str
    probe: Callable[[], Awaitable[None]] | Callable[[], None]
    blocking: bool = False
    min_interval_s: float = 0.0


@dataclass(frozen=True)
class DependencyResult:
    up: bool
    error_code: str | None
    checked_at: float


class DependencyHealthChecker:
    """Probes each dependency in the background and caches the results.

    A probe that does not finish within ``timeout_s`` is reported down
    (``dependency.timeout``). A blocking probe cannot be interrupted: until it returns,
    no second one is started for that dependency, and each round reports it down.
    """

    def __init__(
        self,
        checks: Iterable[DependencyCheck],
        *,
        interval_s: float,
        timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._checks = tuple(checks)
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._results: dict[str, DependencyResult] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._executor: ThreadPoolExecutor | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self._checks)

    def results(self) -> dict[str, DependencyResult]:
        with self._lock:
            return dict(self._results)

    def statuses(self) -> dict[str, str]:
        """``up``, ``down`` or ``unknown`` (not yet checked) for every dependency."""
        results = self.results()
        statuses = {}
        for name in self.names:
            result = results.get(name)
            if result is None:
                statuses[name] = STATUS_UNKNOWN
            else:
                statuses[name] = STATUS_UP if result.up else STATUS_DOWN
        return statuses

    async def check_once(self) -> None:
        """Probe every dependency that is due, concurrently. Never raises."""
        await asyncio.gather(*(self._safely_check(check) for check in self._checks))

    async def run(self) -> None:
        """Check, then sleep for the interval, until cancelled. Never raises otherwise."""
        logger.info("Dependency health checker started (interval=%ss)", self._interval_s)
        try:
            while True:
                await self.check_once()
                await asyncio.sleep(self._interval_s)
        except asyncio.CancelledError:
            logger.info("Dependency health checker cancelled")
            raise
        finally:
            self.close()
            await self._close_probes()

    async def _close_probes(self) -> None:
        """Release what probes hold open (e.g. the storage credential's HTTP session)."""
        for check in self._checks:
            aclose = getattr(check.probe, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception as exc:
                logger.warning("Could not close the %s probe (%s)", check.name, type(exc).__name__)

    def close(self) -> None:
        """Release the probe threads (a later run starts new ones)."""
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    async def _safely_check(self, check: DependencyCheck) -> None:
        try:
            await self._check(check)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the checker itself must never fail
            logger.warning("Dependency health check of %s failed (%s)", check.name, type(exc).__name__)

    async def _check(self, check: DependencyCheck) -> None:
        previous = self.results().get(check.name)
        if previous is not None and self._clock() - previous.checked_at < check.min_interval_s:
            return
        pending = self._inflight.get(check.name)
        if pending is not None and not pending.done():
            # The last blocking probe is still running: still down, and no second one.
            self._update(check.name, None, CODE_TIMEOUT)
            return
        self._inflight.pop(check.name, None)

        started = time.perf_counter()
        future = self._start(check)
        try:
            done, _ = await asyncio.wait({future}, timeout=self._timeout_s)
        except asyncio.CancelledError:
            if check.blocking:
                self._inflight[check.name] = future
            else:
                future.cancel()
                await asyncio.gather(future, return_exceptions=True)
            raise
        seconds = time.perf_counter() - started
        if not done:
            if check.blocking:
                self._inflight[check.name] = future
            else:
                future.cancel()
            code: str | None = CODE_TIMEOUT
        elif future.cancelled():
            code = CODE_ERROR
        else:
            exc = future.exception()
            code = None if exc is None else error_code(exc)
            if exc is not None:
                logger.debug("Dependency %s check failed (%s)", check.name, type(exc).__name__)
        self._update(check.name, seconds, code)

    def _start(self, check: DependencyCheck) -> asyncio.Future:
        if check.blocking:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=sum(item.blocking for item in self._checks),
                    thread_name_prefix="dependency-health",
                )
            future = asyncio.get_running_loop().run_in_executor(self._executor, _untraced_call, check.probe)
        else:
            future = asyncio.ensure_future(_untraced_await(check.probe))
        # A probe abandoned after a timeout may fail later: retrieve its exception.
        future.add_done_callback(_retrieve_exception)
        return future

    def _update(self, name: str, seconds: float | None, code: str | None) -> None:
        up = code is None
        with self._lock:
            previous = self._results.get(name)
            self._results[name] = DependencyResult(up=up, error_code=code, checked_at=self._clock())
        if seconds is not None:
            _record_duration(name, seconds, up)
        changed = (previous is None and not up) or (previous is not None and previous.up != up)
        if changed:
            _report_change(name, up, code)

    # Telemetry -----------------------------------------------------------------------

    def _observe(self, _options: CallbackOptions) -> list[Observation]:
        return [
            Observation(1 if result.up else 0, {ATTR_DEPENDENCY_NAME: name}) for name, result in self.results().items()
        ]

    def register(self, meter: Meter) -> None:
        meter.create_observable_gauge(
            METRIC_DEPENDENCY_UP,
            callbacks=[self._observe],
            unit="1",
            description="1 if the dependency's last health check succeeded, 0 if it failed",
        )


def _untraced_call(probe: Callable[[], None]) -> None:
    # The checker's own telemetry describes these calls: no spans for each probe.
    with suppress_instrumentation():
        probe()


async def _untraced_await(probe: Callable[[], Awaitable[None]]) -> None:
    with suppress_instrumentation():
        await probe()


def _retrieve_exception(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


def _record_duration(name: str, seconds: float, up: bool) -> None:
    from transcribe_api.stt.job_telemetry import _instrument  # cached per meter

    try:
        _instrument(
            "histogram",
            METRIC_DEPENDENCY_CHECK_DURATION,
            unit="s",
            description="Duration of dependency health checks",
        ).record(
            seconds,
            {
                ATTR_DEPENDENCY_NAME: name,
                c.OUTCOME: (Outcome.SUCCESS if up else Outcome.ERROR).value,
            },
        )
    except Exception as exc:
        logger.warning("Could not record dependency check duration (%s)", type(exc).__name__)


def _report_change(name: str, up: bool, code: str | None) -> None:
    attributes: dict[str, object] = {
        ATTR_DEPENDENCY_NAME: name,
        ATTR_DEPENDENCY_STATE: STATUS_UP if up else STATUS_DOWN,
    }
    if code is not None:
        attributes[c.ERROR_CODE] = code
    if up:
        logger.info("Dependency %s is up", name)
    else:
        logger.warning("Dependency %s is down (%s)", name, code)
    record_event(EVENT_DEPENDENCY_HEALTH_CHANGED, attributes)


# Probes ---------------------------------------------------------------------------------


def check_database() -> None:
    """Both areas' connection pools (one database), so exhaustion of either shows."""
    from transcribe_api.runtime.db_dictation import engine as dictation_engine
    from transcribe_api.runtime.db_recording import get_engine as recording_engine

    for engine in (dictation_engine, recording_engine()):
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))


class StorageProbe:
    """Reuses one blob manager, so its managed-identity token is cached between checks."""

    def __init__(self) -> None:
        self._manager = None

    async def __call__(self) -> None:
        settings = get_settings()
        if settings.AUDIO_STORAGE_BACKEND == "local":
            if not Path(settings.LOCAL_AUDIO_STORAGE_DIR).is_dir():
                raise FileNotFoundError
            return
        if self._manager is None:
            from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager

            self._manager = AsyncAzureBlobManager()
        # Both containers: recordings and dictation uploads (audio), and transcripts.
        await self._manager.check_container(settings.AZURE_STORAGE_CONTAINER_NAME)
        await self._manager.check_container(_transcription_container())

    async def aclose(self) -> None:
        manager, self._manager = self._manager, None
        if manager is not None:
            await manager.close()


def _transcription_container() -> str:
    from transcribe_api.runtime.settings_dictation import get_settings as dictation_settings

    return dictation_settings().AZURE_STORAGE_TRANSCRIPTION_CONTAINER


async def check_speech() -> None:
    from transcribe_api.stt.batch_client import check_speech_available

    await check_speech_available(get_settings().DEPENDENCY_HEALTH_TIMEOUT_SECONDS)


def default_checks() -> list[DependencyCheck]:
    checks = [
        DependencyCheck(DATABASE, check_database, blocking=True),
        DependencyCheck(STORAGE, StorageProbe()),
    ]
    settings = get_settings()
    # Not with the mock backend or without an endpoint (local development): there is
    # no Speech to check, and reporting it down would make the endpoint 503 for ever.
    if settings.SPEECH_BACKEND != "mock" and settings.AZURE_SPEECH_ENDPOINT:
        checks.append(DependencyCheck(SPEECH, check_speech, min_interval_s=SPEECH_MIN_INTERVAL_S))
    return checks


# The process's checker ------------------------------------------------------------------

_checker: DependencyHealthChecker | None = None
_checker_lock = threading.Lock()
_registered: list[Meter] = []


def get_dependency_checker() -> DependencyHealthChecker:
    """The process's checker (created on first use; started by the app lifespan)."""
    global _checker
    with _checker_lock:
        if _checker is None:
            settings = get_settings()
            _checker = DependencyHealthChecker(
                default_checks(),
                interval_s=settings.DEPENDENCY_HEALTH_INTERVAL_SECONDS,
                timeout_s=settings.DEPENDENCY_HEALTH_TIMEOUT_SECONDS,
            )
        return _checker


def register_dependency_gauge(checker: DependencyHealthChecker | None = None) -> bool:
    """Register service.dependency.up on the current meter provider, once. True if new."""
    meter = get_meter()
    if any(existing is meter for existing in _registered):
        return False
    (checker or get_dependency_checker()).register(meter)
    _registered.append(meter)
    return True


def health_response(statuses: Mapping[str, str]) -> tuple[int, dict]:
    """503 and ``degraded`` when any dependency is down; otherwise 200 and ``ok``.

    A dependency not yet checked (``unknown``, briefly after startup) is not down, so a
    restart does not report an outage.
    """
    healthy = all(status != STATUS_DOWN for status in statuses.values())
    return (200 if healthy else 503), {
        "status": "ok" if healthy else "degraded",
        "dependencies": dict(statuses),
    }
