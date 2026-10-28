"""DIAAT-277: service and dependency health.

1. A controlled dependency failure is visible: dependency.health_changed (once per
   up/down change), the service.dependency.up gauge and the check-duration histogram.
2. GET /api/v1/health/dependencies serves the cached results (200 or 503) and never
   probes a dependency; /api/v1/health stays static (it is the eviction probe).
3. The polling loop's heartbeat (transcription.poller.last_success_age).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from azure.core.exceptions import ServiceResponseTimeoutError
from sqlalchemy import exc as sa_exc
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from fastapi.testclient import TestClient
from hmcts_otel import conventions as c
from hmcts_otel import get_meter
from hmcts_otel.testing import capture_telemetry
from sqlalchemy.exc import OperationalError

from transcribe_api.runtime import dependency_health
from transcribe_api.stt import job_telemetry
from transcribe_api.stt.batch_client import SpeechNotConfiguredError
from transcribe_api.stt.job_telemetry import METRIC_POLLER_LAST_SUCCESS_AGE, PollerHeartbeat
from transcribe_api.runtime.dependency_health import (
    METRIC_DEPENDENCY_CHECK_DURATION,
    METRIC_DEPENDENCY_UP,
    DependencyCheck,
    DependencyHealthChecker,
    error_code,
)
from transcribe_api.runtime.telemetry import EVENT_DEPENDENCY_HEALTH_CHANGED, configure_telemetry

SECRET_HOST = "bat-dev-psql.postgres.database.azure.com"
SECRET_KEY = "s3cr3t-speech-key-value"  # pragma: allowlist secret
SECRET_URL = f"https://{SECRET_HOST}/speechtotext?key={SECRET_KEY}"


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


class Switch:
    """A probe that fails while ``error`` is set, counting its calls."""

    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def _checker(*checks: DependencyCheck, timeout_s: float = 1.0, clock=None):
    kwargs = {"clock": clock} if clock is not None else {}
    return DependencyHealthChecker(checks, interval_s=60, timeout_s=timeout_s, **kwargs)


def _changes(tel):
    return tel.events(EVENT_DEPENDENCY_HEALTH_CHANGED)


def _up_values(tel):
    return {p["attributes"]["dependency.name"]: p["value"] for p in tel.metric_points(METRIC_DEPENDENCY_UP)}


# Transitions ----------------------------------------------------------------------------


async def test_each_change_between_up_and_down_is_reported_once(tel):
    probe = Switch()
    checker = _checker(DependencyCheck("database", probe))

    await checker.check_once()  # first check, up: not a change
    await checker.check_once()
    assert _changes(tel) == []

    probe.error = OperationalError("SELECT 1", {}, ConnectionRefusedError())
    await checker.check_once()
    await checker.check_once()  # still down: no second event
    probe.error = None
    await checker.check_once()
    await checker.check_once()

    changes = _changes(tel)
    assert [(e["dependency.name"], e["dependency.state"]) for e in changes] == [
        ("database", "down"),
        ("database", "up"),
    ]
    assert changes[0][c.ERROR_CODE] == "dependency.unreachable"
    assert c.ERROR_CODE not in changes[1]
    assert probe.calls == 6


async def test_down_on_the_first_check_is_reported(tel):
    checker = _checker(DependencyCheck("storage", AsyncMock(side_effect=ServiceRequestError("x"))))
    await checker.check_once()
    (change,) = _changes(tel)
    assert change["dependency.state"] == "down"
    assert change[c.ERROR_CODE] == "dependency.unreachable"


# Metrics --------------------------------------------------------------------------------


async def test_gauge_reflects_the_cache(tel):
    database, storage = Switch(), Switch()
    checker = _checker(DependencyCheck("database", database), DependencyCheck("storage", storage))
    checker.register(get_meter())

    assert tel.metric_points(METRIC_DEPENDENCY_UP) == []  # never checked: no observation
    storage.error = ClientAuthenticationError("denied")
    await checker.check_once()
    assert _up_values(tel) == {"database": 1, "storage": 0}

    storage.error = None
    database.error = TimeoutError()
    await checker.check_once()
    assert _up_values(tel) == {"database": 0, "storage": 1}


async def test_check_duration_is_recorded_by_dependency_and_outcome(tel):
    checker = _checker(
        DependencyCheck("database", AsyncMock()),
        DependencyCheck("speech", AsyncMock(side_effect=RuntimeError())),
    )
    await checker.check_once()
    points = {
        (p["attributes"]["dependency.name"], p["attributes"][c.OUTCOME]): p["count"]
        for p in tel.metric_points(METRIC_DEPENDENCY_CHECK_DURATION)
    }
    assert points == {("database", "success"): 1, ("speech", "error"): 1}
    for p in tel.metric_points(METRIC_DEPENDENCY_CHECK_DURATION):
        assert set(p["attributes"]) == {"dependency.name", c.OUTCOME}


def test_gauge_is_registered_once_per_meter(tel):
    checker = _checker(DependencyCheck("database", AsyncMock()))
    assert dependency_health.register_dependency_gauge(checker) is True
    assert dependency_health.register_dependency_gauge(checker) is False


# Timeouts and throttling -----------------------------------------------------------------


async def test_a_hanging_async_check_is_bounded_by_the_timeout(tel):
    async def hang() -> None:
        await asyncio.sleep(3600)

    checker = _checker(DependencyCheck("speech", hang), timeout_s=0.05)
    started = time.monotonic()
    await asyncio.wait_for(checker.check_once(), timeout=2)
    assert time.monotonic() - started < 1
    assert checker.statuses() == {"speech": "down"}
    assert _changes(tel)[0][c.ERROR_CODE] == "dependency.timeout"


async def test_a_hanging_blocking_check_is_bounded_and_not_started_twice(tel):
    release = threading.Event()
    calls = []

    def hang() -> None:
        calls.append(1)
        release.wait(5)

    checker = _checker(DependencyCheck("database", hang, blocking=True), timeout_s=0.05)
    try:
        await asyncio.wait_for(checker.check_once(), timeout=2)
        assert checker.statuses() == {"database": "down"}
        await asyncio.wait_for(checker.check_once(), timeout=2)  # previous still running
        assert calls == [1]
        assert checker.statuses() == {"database": "down"}
    finally:
        release.set()
    await asyncio.sleep(0.1)  # the abandoned probe returns
    await checker.check_once()
    assert calls == [1, 1]
    assert checker.statuses() == {"database": "up"}
    assert [e["dependency.state"] for e in _changes(tel)] == ["down", "up"]
    checker.close()


async def test_speech_is_checked_at_most_once_per_300_seconds(tel):
    now = [1000.0]
    probe = Switch()
    checker = _checker(
        DependencyCheck("speech", probe, min_interval_s=dependency_health.SPEECH_MIN_INTERVAL_S),
        clock=lambda: now[0],
    )
    await checker.check_once()
    now[0] += 299
    await checker.check_once()
    assert probe.calls == 1
    now[0] += 1
    await checker.check_once()
    assert probe.calls == 2


def test_default_checks_throttle_only_speech():
    checks = {check.name: check for check in dependency_health.default_checks()}
    assert list(checks) == ["database", "storage", "speech"]
    assert checks["speech"].min_interval_s == 300
    assert checks["database"].min_interval_s == checks["storage"].min_interval_s == 0
    assert checks["database"].blocking is True


async def test_the_checker_never_raises(tel):
    checker = _checker(DependencyCheck("database", AsyncMock(side_effect=BaseException)))
    with patch.object(checker, "_check", side_effect=RuntimeError("bug")):
        await checker.check_once()  # no exception


async def test_run_checks_until_cancelled(monkeypatch):
    checker = _checker(DependencyCheck("database", AsyncMock()))
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with patch.object(checker, "check_once", new=AsyncMock()) as check_once:
        with pytest.raises(asyncio.CancelledError):
            await checker.run()
    assert check_once.await_count == 2
    sleep.assert_awaited_with(60)


# Error codes and content ------------------------------------------------------------------


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://speech.example/transcriptions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("x", request=request, response=response)


class _PgError(Exception):
    pgcode = "28P01"  # invalid_password


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (TimeoutError(), "dependency.timeout"),
        (httpx.ReadTimeout("x"), "dependency.timeout"),
        (_status_error(401), "dependency.auth_failed"),
        (_status_error(403), "dependency.auth_failed"),
        (ClientAuthenticationError("x"), "dependency.auth_failed"),
        (OperationalError("SELECT 1", {}, _PgError()), "dependency.auth_failed"),
        (_status_error(503), "dependency.error"),
        (ResourceNotFoundError("x"), "dependency.error"),
        (HttpResponseError("x"), "dependency.error"),
        (httpx.ConnectError("x"), "dependency.unreachable"),
        (ServiceRequestError("x"), "dependency.unreachable"),
        (OperationalError("SELECT 1", {}, ConnectionRefusedError()), "dependency.unreachable"),
        (ConnectionRefusedError(), "dependency.unreachable"),
        (SpeechNotConfiguredError(), "dependency.error"),
        (RuntimeError(), "dependency.error"),
    ],
)
def test_error_codes_are_controlled(exc, code):
    assert error_code(exc) == code


async def test_no_hostnames_keys_urls_or_messages_in_telemetry(tel, caplog):
    error = OperationalError(f"SELECT 1 -- {SECRET_URL}", {}, Exception(f"could not connect to {SECRET_HOST}"))
    checker = _checker(
        DependencyCheck("database", AsyncMock(side_effect=error)),
        DependencyCheck("speech", AsyncMock(side_effect=RuntimeError(SECRET_URL))),
    )
    checker.register(get_meter())
    with caplog.at_level(logging.DEBUG, logger="transcription_svc"):
        await checker.check_once()
        tel.metric_points(METRIC_DEPENDENCY_UP)
    assert len(_changes(tel)) == 2
    tel.assert_no_prohibited_content(SECRET_HOST, SECRET_KEY, SECRET_URL, "could not connect")
    for needle in (SECRET_HOST, SECRET_KEY, "could not connect"):
        assert needle not in caplog.text


# Probes -----------------------------------------------------------------------------------


async def test_speech_probe_lists_one_base_model_with_the_workload_identity(monkeypatch, speech_workload_identity):
    from transcribe_api.stt.batch_client import check_speech_available
    from transcribe_api.runtime.settings_recording import get_settings

    monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "https://test.cognitiveservices.azure.com/")
    get_settings.cache_clear()

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"values": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    await check_speech_available(5)
    (request,) = seen
    assert request.method == "GET"
    # The service catalogue: no audio, nothing billed.
    assert str(request.url) == ("https://test.cognitiveservices.azure.com/speechtotext/v3.2/models/base?top=1")
    assert request.headers["Authorization"].startswith("Bearer ")
    assert "Ocp-Apim-Subscription-Key" not in request.headers


async def test_speech_probe_without_a_token_is_an_auth_failure(speech_workload_identity):
    from transcribe_api.stt.batch_client import check_speech_available
    from transcribe_api.stt.speech_auth import SpeechAuthError

    speech_workload_identity.error = RuntimeError("no federated token")
    with pytest.raises(SpeechAuthError) as raised:
        await check_speech_available(5)
    assert error_code(raised.value) == "dependency.auth_failed"


async def test_speech_probe_raises_on_401(monkeypatch):
    from transcribe_api.stt.batch_client import check_speech_available

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(lambda _r: httpx.Response(401)), **kw),
    )
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await check_speech_available(5)
    assert error_code(raised.value) == "dependency.auth_failed"


async def test_speech_probe_without_configuration_fails(monkeypatch):
    from transcribe_api.stt.batch_client import (
        SpeechNotConfiguredError,
        check_speech_available,
    )
    from transcribe_api.runtime.settings_recording import get_settings

    monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "")
    get_settings.cache_clear()
    with pytest.raises(SpeechNotConfiguredError):
        await check_speech_available(5)


async def test_storage_probe_reads_the_container_properties_untraced():
    from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager

    container = MagicMock()
    container.get_container_properties = AsyncMock()
    service = MagicMock()
    service.get_container_client.return_value = container
    service.__aenter__ = AsyncMock(return_value=service)
    service.__aexit__ = AsyncMock(return_value=False)
    with patch.object(AsyncAzureBlobManager, "_blob_service_client", return_value=service):
        await dependency_health.StorageProbe()()
    # Both containers: audio, and the dictation transcripts.
    names = [call.args[0] for call in service.get_container_client.call_args_list]
    assert names == ["test-container", "test-transcription-container"]
    container.get_container_properties.assert_awaited_with(tracing_options={"enabled": False})


async def test_storage_probe_reuses_its_blob_manager():
    probe = dependency_health.StorageProbe()
    manager = MagicMock()
    manager.check_container = AsyncMock()
    with patch("transcribe_api.runtime.blob_recording.AsyncAzureBlobManager", return_value=manager) as cls:
        await probe()
        await probe()
    cls.assert_called_once_with()
    assert manager.check_container.await_count == 4  # two containers, twice


async def test_local_storage_probe_checks_the_directory(monkeypatch, tmp_path):
    from transcribe_api.runtime.settings_recording import get_settings

    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    await dependency_health.StorageProbe()()
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path / "missing"))
    get_settings.cache_clear()
    with pytest.raises(FileNotFoundError):
        await dependency_health.StorageProbe()()


def test_database_probe_runs_select_1_on_both_pools():
    connections = []

    def engine():
        connection = MagicMock()
        connections.append(connection)
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        return engine

    with (
        patch("transcribe_api.runtime.db_dictation.engine", engine()),
        patch("transcribe_api.runtime.db_recording.get_engine", return_value=engine()),
    ):
        dependency_health.check_database()
    assert [str(c.execute.call_args.args[0]) for c in connections] == ["SELECT 1", "SELECT 1"]


def test_the_speech_check_is_skipped_with_the_mock_backend(monkeypatch):
    from transcribe_api.runtime.settings_recording import get_settings

    monkeypatch.setenv("SPEECH_BACKEND", "mock")
    get_settings.cache_clear()
    assert [check.name for check in dependency_health.default_checks()] == ["database", "storage"]


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (sa_exc.TimeoutError("pool"), "dependency.timeout"),
        (
            sa_exc.OperationalError("connect", {}, Exception('FATAL:  password authentication failed for user "x"')),
            "dependency.auth_failed",
        ),
        (ServiceResponseTimeoutError("slow"), "dependency.timeout"),
    ],
)
def test_pool_timeouts_azure_timeouts_and_libpq_auth_failures_are_classified(exc, code):
    assert error_code(exc) == code


# Endpoint ---------------------------------------------------------------------------------


@pytest.fixture
def client_with_checker(monkeypatch):
    """A TestClient whose checker has probes that fail the test if called."""

    def make(results: dict[str, bool]):
        never = AsyncMock(side_effect=AssertionError("the endpoint must not probe"))
        names = ("database", "storage", "speech")
        checker = _checker(*(DependencyCheck(name, never) for name in names))
        for name, up in results.items():
            checker._update(name, None, None if up else "dependency.error")
        monkeypatch.setattr(dependency_health, "_checker", checker)
        from transcribe_api.api.app import create_app

        return TestClient(create_app()), never

    return make


def test_endpoint_is_200_when_every_dependency_is_up(client_with_checker):
    client, probe = client_with_checker({"database": True, "storage": True, "speech": True})
    response = client.get("/api/health/dependencies")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "dependencies": {"database": "up", "storage": "up", "speech": "up"},
    }
    probe.assert_not_called()


def test_endpoint_is_503_when_a_dependency_is_down(client_with_checker):
    client, probe = client_with_checker({"database": True, "storage": False, "speech": True})
    response = client.get("/api/health/dependencies")
    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "dependencies": {"database": "up", "storage": "down", "speech": "up"},
    }
    probe.assert_not_called()


def test_a_dependency_not_yet_checked_is_not_an_outage(client_with_checker):
    client, _ = client_with_checker({"database": True})
    response = client.get("/api/health/dependencies")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["dependencies"] == {
        "database": "up",
        "storage": "unknown",
        "speech": "unknown",
    }


def test_health_stays_static_with_a_dependency_down(client_with_checker):
    client, probe = client_with_checker({"database": False, "storage": False, "speech": False})
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    probe.assert_not_called()


def test_endpoint_is_excluded_from_request_telemetry(client_with_checker, monkeypatch):
    from transcribe_api.runtime import settings_dictation

    # The suite turns FastAPI instrumentation off; this test needs request spans.
    monkeypatch.setenv("DISABLE_FASTAPI_INSTRUMENTATION", "false")
    settings_dictation.get_settings.cache_clear()
    with capture_telemetry() as tel:
        configure_telemetry()
        client, _ = client_with_checker({"database": True, "storage": True, "speech": True})
        client.get("/api/health/dependencies")
        client.get("/api/v1/not-a-route")  # a traced request, for contrast
        routes = [s.attributes.get("url.path") or s.name for s in tel.finished_spans()]
    assert not any("health" in str(route) for route in routes)
    assert routes  # the contrast request was traced


def test_app_starts_the_checker_outside_tests(monkeypatch):
    from transcribe_api.api.app import create_app
    from transcribe_api.stt.polling_service import BatchPollingService
    from transcribe_api.runtime.settings_recording import get_settings

    monkeypatch.setenv("ENVIRONMENT", "dev")
    get_settings.cache_clear()
    checker = MagicMock()
    checker.run = AsyncMock()
    monkeypatch.setattr(dependency_health, "_checker", checker)
    with (
        patch.object(job_telemetry, "register_job_gauges"),
        patch.object(dependency_health, "register_dependency_gauge") as register,
        patch.object(BatchPollingService, "run_polling_loop", new=AsyncMock()),
        TestClient(create_app()),
    ):
        pass
    register.assert_called_once_with()
    checker.run.assert_awaited_once_with()


def test_app_does_not_start_the_checker_in_tests(monkeypatch):
    from transcribe_api.api.app import create_app

    checker = MagicMock()
    checker.run = AsyncMock()
    monkeypatch.setattr(dependency_health, "_checker", checker)
    with TestClient(create_app()):
        pass
    checker.run.assert_not_called()


@pytest.mark.parametrize("name", ["DEPENDENCY_HEALTH_INTERVAL_SECONDS", "DEPENDENCY_HEALTH_TIMEOUT_SECONDS"])
def test_dependency_health_settings_must_be_positive(monkeypatch, name):
    from pydantic import ValidationError

    from transcribe_api.runtime.settings_recording import Settings

    monkeypatch.setenv(name, "0")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv(name, "")
    assert getattr(Settings(), name) == Settings.model_fields[name].default


# Poller heartbeat -------------------------------------------------------------------------


def test_heartbeat_age_since_start_then_since_last_success(tel):
    now = [100.0]
    heartbeat = PollerHeartbeat(clock=lambda: now[0])
    heartbeat.register(get_meter())
    assert tel.metric_points(METRIC_POLLER_LAST_SUCCESS_AGE) == []  # loop not started

    heartbeat.started()
    now[0] = 130.0
    assert [p["value"] for p in tel.metric_points(METRIC_POLLER_LAST_SUCCESS_AGE)] == [30.0]
    heartbeat.succeeded()
    now[0] = 145.0
    heartbeat.started()  # a restart does not reset the age
    assert [p["value"] for p in tel.metric_points(METRIC_POLLER_LAST_SUCCESS_AGE)] == [15.0]


async def test_polling_loop_beats_only_on_a_successful_poll(monkeypatch):
    from transcribe_api.stt.polling_service import BatchPollingService

    heartbeat = MagicMock()
    monkeypatch.setattr(job_telemetry, "poller_heartbeat", heartbeat)
    service = BatchPollingService()
    polls = AsyncMock(side_effect=[None, RuntimeError("boom")])
    monkeypatch.setattr(service, "_poll_once", polls)
    monkeypatch.setattr(service, "_report_stuck_jobs", AsyncMock())
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()]))

    with pytest.raises(asyncio.CancelledError):
        await service.run_polling_loop()
    heartbeat.started.assert_called_once_with()
    assert heartbeat.succeeded.call_count == 1  # the second poll failed


def test_heartbeat_is_registered_with_the_job_gauges(tel):
    job_telemetry.register_job_gauges(MagicMock())
    with patch.object(job_telemetry.poller_heartbeat, "age", return_value=12.0):
        assert [p["value"] for p in tel.metric_points(METRIC_POLLER_LAST_SUCCESS_AGE)] == [12.0]


async def test_probes_run_with_instrumentation_suppressed(tel):
    """The checker's own metrics describe the probes: no span per probe every minute."""
    from opentelemetry.instrumentation.utils import is_instrumentation_enabled

    seen: dict[str, bool] = {}

    async def async_probe() -> None:
        seen["async"] = is_instrumentation_enabled()

    def blocking_probe() -> None:
        seen["blocking"] = is_instrumentation_enabled()

    checker = _checker(
        DependencyCheck("storage", async_probe),
        DependencyCheck("database", blocking_probe, blocking=True),
    )
    await checker.check_once()
    checker.close()
    assert seen == {"async": False, "blocking": False}


async def test_a_closed_checker_can_check_again(tel):
    probe = MagicMock()
    checker = _checker(DependencyCheck("database", probe, blocking=True))
    await checker.check_once()
    checker.close()
    await checker.check_once()
    checker.close()
    assert probe.call_count == 2
    assert checker.statuses() == {"database": "up"}
