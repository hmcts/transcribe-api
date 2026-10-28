"""Telemetry setup for the API and the worker (DIAAT-205, runtime/telemetry.py)."""

from __future__ import annotations

import logging
import re
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from hmcts_otel import TelemetrySettings, default_redactor

from transcribe_api.runtime import telemetry

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch):
    """configure_telemetry with setup_telemetry captured instead of run."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("HMCTS_OTEL_SERVICE_NAME", raising=False)
    calls: list[TelemetrySettings] = []
    with patch.object(telemetry, "setup_telemetry", side_effect=lambda s: calls.append(s) or s):
        yield calls


def test_the_api_reports_as_transcribe_api(configured: list[TelemetrySettings]) -> None:
    telemetry.configure_telemetry()
    assert configured[-1].service_name == "transcribe-api"


def test_the_worker_reports_as_transcribe_api_worker(configured: list[TelemetrySettings]) -> None:
    telemetry.configure_telemetry(telemetry.SERVICE_NAME_WORKER)
    assert configured[-1].service_name == "transcribe-api-worker"


def test_otel_service_name_wins(configured: list[TelemetrySettings], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SERVICE_NAME", "transcribe-api-canary")
    telemetry.configure_telemetry(telemetry.SERVICE_NAME_WORKER)
    assert configured[-1].service_name == "transcribe-api-canary"


def test_sentry_is_never_enabled(configured: list[TelemetrySettings], monkeypatch: pytest.MonkeyPatch) -> None:
    # No Sentry on CNP: a DSN left in the environment must not switch it on.
    monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.example.test/1")
    telemetry.configure_telemetry()
    assert configured[-1].sentry_dsn is None


def test_the_version_and_environment_are_reported(configured: list[TelemetrySettings]) -> None:
    telemetry.configure_telemetry()
    assert configured[-1].service_version == telemetry.APP_VERSION
    assert configured[-1].environment == "test"


EXPORTER_LOGGER = "azure.monitor.opentelemetry.exporter.export._base"
HTTP_LOGGING_POLICY_LOGGER = "azure.core.pipeline.policies.http_logging_policy"


@pytest.fixture
def exported_logs(monkeypatch: pytest.MonkeyPatch):
    """configure_telemetry exporting logs to memory, starting from the logging state of
    a fresh process: nothing has lowered the Azure SDK loggers yet."""
    from hmcts_otel import setup_telemetry, shutdown_telemetry
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    names = ("azure", "azure.monitor.opentelemetry.exporter", HTTP_LOGGING_POLICY_LOGGER, EXPORTER_LOGGER)
    levels = {name: logging.getLogger(name).level for name in names}
    for name in names:
        logging.getLogger(name).setLevel(logging.NOTSET)
    shutdown_telemetry()
    exporter = InMemoryLogRecordExporter()
    monkeypatch.setattr(
        telemetry,
        "setup_telemetry",
        lambda settings: setup_telemetry(settings, log_exporter=exporter, set_global_providers=False),
    )
    try:
        yield exporter
    finally:
        shutdown_telemetry()
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


def test_the_exporter_does_not_export_its_own_upload_logs(exported_logs) -> None:
    """Exporting the exporter's (and its HTTP pipeline's) per-upload logs triggers
    another upload, which logs again: a feedback loop."""
    telemetry.configure_telemetry(telemetry.SERVICE_NAME_WORKER)
    logging.getLogger("transcribe_api.stt.work_poller").info("poller started")
    logging.getLogger(EXPORTER_LOGGER).info("Transmission succeeded: Item received: 1. Items accepted: 1")
    logging.getLogger(HTTP_LOGGING_POLICY_LOGGER).info(
        "Request URL: 'https://uksouth-1.in.applicationinsights.azure.com/v2.1/track'"
    )

    bodies = [item.log_record.body for item in exported_logs.get_finished_logs()]
    assert "poller started" in bodies
    assert not [body for body in bodies if "Transmission succeeded" in str(body) or "Request URL" in str(body)]


def test_the_worker_entrypoint_configures_telemetry_as_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    # Run as `python -m`, from a clean import: runpy warns if it is already loaded.
    monkeypatch.delitem(sys.modules, "transcribe_api.worker", raising=False)
    with (
        patch("transcribe_api.runtime.telemetry.configure_telemetry") as configure,
        patch("asyncio.run") as run,
    ):
        runpy.run_module("transcribe_api.worker", run_name="__main__")
    configure.assert_called_once_with("transcribe-api-worker")
    run.assert_called_once()
    run.call_args.args[0].close()  # the main() coroutine, never awaited here


def test_the_worker_flushes_telemetry_when_it_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "transcribe_api.worker", raising=False)
    with (
        patch("transcribe_api.runtime.telemetry.configure_telemetry"),
        patch("asyncio.run", side_effect=lambda coro: coro.close()),
        patch("hmcts_otel.shutdown_telemetry") as shutdown,
    ):
        runpy.run_module("transcribe_api.worker", run_name="__main__")
    shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_sigterm_stops_the_pollers_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kubernetes stops the worker with SIGTERM. The handler must cancel the main task,
    so the pollers are stopped and telemetry flushed, rather than the process dying.
    The handler is captured and called, not a real signal: without it, a real SIGTERM
    would kill the test run."""
    import asyncio
    import signal

    from transcribe_api import worker

    handlers: dict[int, object] = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.__setitem__(sig, callback))
    stopped: list[list] = []

    async def start():
        return [asyncio.create_task(asyncio.sleep(3600))]

    async def stop(tasks):
        for task in tasks:
            task.cancel()
        stopped.append(tasks)

    monkeypatch.setattr(worker, "ensure_ffmpeg_tools", lambda _env: asyncio.sleep(0))
    monkeypatch.setattr(worker, "start_background_workers", start)
    monkeypatch.setattr(worker, "stop_background_workers", stop)

    running = asyncio.create_task(worker.main())
    for _ in range(100):  # until main() is running and has registered its handler
        if signal.SIGTERM in handlers:
            break
        await asyncio.sleep(0)
    if signal.SIGTERM not in handlers:
        running.cancel()
        pytest.fail("worker.main() registered no SIGTERM handler")
    handlers[signal.SIGTERM]()  # what the event loop does on SIGTERM
    await asyncio.wait_for(running, timeout=5)

    assert len(stopped) == 1
    assert running.done() and not running.cancelled()


# Redaction ----------------------------------------------------------------------------

UPLOAD_ID = "3f2a9c1e-8b7d-4e6f-9a1b-2c3d4e5f6a7b"
USER_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def test_redaction_rules_are_registered_upload_file_names_first() -> None:
    telemetry.register_redaction_rules()
    telemetry.register_redaction_rules()  # idempotent
    names = [rule.name for rule in default_redactor.rules]
    assert names.count("upload_file_name") == 1
    assert names.count("appeal_reference") == 1
    assert names.index("upload_file_name") < names.index("appeal_reference")


@pytest.mark.parametrize(
    "text",
    [
        f"uploads/{USER_ID}/{UPLOAD_ID}-Smith_v_Home_Office.wav",
        f"https://acct.blob.core.windows.net/audio/uploads%2F{USER_ID}%2F{UPLOAD_ID}-PA_50123_2024.wav?sv=1",
    ],
)
def test_upload_file_names_are_removed_but_the_ids_kept(text: str) -> None:
    telemetry.register_redaction_rules()
    redacted = default_redactor.redact(text)
    assert "Smith" not in redacted and "50123" not in redacted
    assert USER_ID in redacted and UPLOAD_ID in redacted


@pytest.mark.parametrize("reference", ["PA/50123/2024", "hearing_PA_50123_2024_part1.mp3", "pa/50123/2024"])
def test_appeal_references_are_redacted(reference: str) -> None:
    telemetry.register_redaction_rules()
    assert "50123" not in default_redactor.redact(f"processing {reference} now")


# Image ----------------------------------------------------------------------------------


def test_the_image_keeps_the_base_image_otel_distro_off_the_path() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    paths = re.findall(r"PYTHONPATH=(\S+)", dockerfile)
    assert paths, "Dockerfile sets no PYTHONPATH"
    for path in paths:
        assert "/opt/otel" not in path.split(":")


# Version ------------------------------------------------------------------------------


@pytest.fixture
def build_version_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from transcribe_api.runtime import version

    path = tmp_path / "BUILD_VERSION"
    monkeypatch.setattr(version, "BUILD_VERSION_FILE", path)
    monkeypatch.delenv("APP_VERSION", raising=False)
    return path


def test_the_image_reports_the_commit_it_was_built_from(build_version_file: Path) -> None:
    from transcribe_api.runtime import version

    build_version_file.write_text("95b8beb")
    assert version.read_version() == "95b8beb"


def test_app_version_overrides_the_built_version(build_version_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from transcribe_api.runtime import version

    build_version_file.write_text("95b8beb")
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    assert version.read_version() == "1.2.3"


def test_a_local_run_reports_the_local_version(build_version_file: Path) -> None:
    from transcribe_api.runtime import version

    assert version.read_version() == version.LOCAL_VERSION


def test_the_pipeline_writes_the_version_where_the_app_reads_it() -> None:
    from transcribe_api.runtime import version

    written = re.findall(r"writeFile file: '([^']+)'", (ROOT / "Jenkinsfile_CNP").read_text())
    assert version.BUILD_VERSION_FILE.relative_to(ROOT).as_posix() in written


# The app, instrumented ---------------------------------------------------------------

REQUEST_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.fixture
def instrumented_client(monkeypatch: pytest.MonkeyPatch):
    """The real app with FastAPI instrumentation on (the suite turns it off), exporting
    to memory. create_app's own configure_telemetry() keeps this capture: setup is
    idempotent."""
    from fastapi.testclient import TestClient
    from hmcts_otel.testing import capture_telemetry

    from transcribe_api.api.app import create_app
    from transcribe_api.runtime import settings_dictation

    monkeypatch.setenv("DISABLE_FASTAPI_INSTRUMENTATION", "false")
    settings_dictation.get_settings.cache_clear()
    with capture_telemetry() as tel:
        app = create_app()

        @app.get("/__test_boom")
        def _boom():
            message = "judge.smith@example.test said PA/50123/2024"
            raise ValueError(message)

        yield TestClient(app, raise_server_exceptions=False), tel


def _server_spans(tel) -> list:
    from opentelemetry.trace import SpanKind

    return [s for s in tel.finished_spans() if s.kind is SpanKind.SERVER]


def test_requests_are_traced_with_their_request_id(instrumented_client) -> None:
    client, tel = instrumented_client
    assert client.get("/api/openapi.json", headers={"X-Request-Id": REQUEST_ID}).status_code == 200

    spans = _server_spans(tel)
    assert len(spans) == 1
    assert spans[0].attributes["hmcts.request.id"] == REQUEST_ID


def test_health_probes_are_not_traced(instrumented_client) -> None:
    client, tel = instrumented_client
    assert client.get("/health").status_code == 200
    assert _server_spans(tel) == []


def test_an_unhandled_error_fails_the_request_span_without_its_content(instrumented_client) -> None:
    from opentelemetry.trace import StatusCode

    client, tel = instrumented_client
    response = client.get("/__test_boom", headers={"X-Request-Id": REQUEST_ID})
    assert response.status_code == 500

    (span,) = _server_spans(tel)
    assert span.status.status_code is StatusCode.ERROR
    exceptions = [e for e in span.events if e.name == "exception"]
    assert exceptions
    assert all(e.attributes["exception.type"] == "ValueError" for e in exceptions)
    # The message (and the stack trace's copy of it) is redacted: it can carry content.
    tel.assert_no_prohibited_content("judge.smith@example.test", "PA/50123/2024")
