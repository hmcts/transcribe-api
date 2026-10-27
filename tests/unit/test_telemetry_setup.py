"""Telemetry setup for the API and the worker (DIAAT-205, runtime/telemetry.py)."""

from __future__ import annotations

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
    # The HMCTS base image's /opt/otel/sitecustomize.py runs configure_azure_monitor()
    # at interpreter start whenever the connection string is set. On PYTHONPATH it
    # would configure Azure Monitor a second time, beside hmcts-otel, and its older
    # OpenTelemetry packages would shadow the versions in uv.lock.
    dockerfile = (ROOT / "Dockerfile").read_text()
    paths = re.findall(r"PYTHONPATH=(\S+)", dockerfile)
    assert paths, "Dockerfile sets no PYTHONPATH"
    for path in paths:
        assert "/opt/otel" not in path.split(":")


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
