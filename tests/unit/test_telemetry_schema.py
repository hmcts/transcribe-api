"""The merged telemetry schema (DIAAT-205, runtime/telemetry_schema.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hmcts_otel import TelemetrySchema, default_schema
from hmcts_otel.schema import EventClass

from transcribe_api.runtime.telemetry_schema import (
    AUDIT_ACTIONS,
    register_schema,
    render_contract,
)

CONTRACT = Path(__file__).resolve().parents[2] / "telemetry-contract.json"


@pytest.fixture(scope="module")
def schema() -> TelemetrySchema:
    return register_schema(TelemetrySchema())


def test_registers_without_conflict_and_twice(schema: TelemetrySchema) -> None:
    # The two heritage schemas raised ValueError when registered into one process.
    register_schema(schema)
    register_schema(default_schema)
    register_schema(default_schema)


def test_every_event_lists_only_registered_attributes(schema: TelemetrySchema) -> None:
    for name, event in schema.events.items():
        unknown = [a for a in event.attributes if schema.get_attribute(a) is None]
        assert not unknown, f"{name}: {unknown}"


# The decisions in the design note -----------------------------------------------------


def test_audit_actions_are_the_auth_library_values_then_app_actions(schema: TelemetrySchema) -> None:
    allowed = schema.get_attribute("audit.action").allowed_values
    assert allowed == frozenset(AUDIT_ACTIONS)
    assert {"ACCESS_DENIED", "ROLE_CHANGE", "transcript.view", "job.submit", "user.delete"} <= allowed
    assert not {"TRANSCRIPT_VIEWED", "TRANSCRIPT_EXPORTED", "TRANSCRIPT_DELETED", "USER_DELETED"} & allowed
    app_actions = allowed - {"ACCESS_DENIED", "ROLE_CHANGE"}
    assert all(a == a.lower() and a.count(".") == 1 for a in app_actions)


@pytest.mark.parametrize(
    ("attribute", "values"),
    [
        ("security.action", {"read", "write", "delete", "submit", "reassign"}),
        (
            "security.resource_type",
            {"audio", "document", "draft", "minute_version", "transcription", "transcription_job"}
            | {"job", "transcript", "upload"},
        ),
    ],
)
def test_shared_vocabularies_are_the_union_of_both_services(
    schema: TelemetrySchema, attribute: str, values: set[str]
) -> None:
    assert schema.get_attribute(attribute).allowed_values == frozenset(values)


def test_speech_error_code_accepts_sdk_names_and_rest_codes(schema: TelemetrySchema) -> None:
    spec = schema.get_attribute("speech.error_code")
    for value in ("ConnectionFailure", "ServiceTimeout", "InvalidPayload", "Throttled.Request"):
        assert spec.check(value, schema.redactor) == (value, None)
    assert spec.check("not a code!", schema.redactor)[0] is None


@pytest.mark.parametrize(
    ("event", "dictation", "recording"),
    [
        ("job.finished", set(), {"job.queue_wait_ms", "job.processing_ms", "speech.error_code"}),
        ("transcript.available", {"dictation.transcription_id"}, {"transcript.segment_count"}),
    ],
)
def test_events_both_services_emit_keep_both_sets_of_attributes(
    schema: TelemetrySchema, event: str, dictation: set[str], recording: set[str]
) -> None:
    # Registered with recording's list alone, dictation's attributes were dropped.
    attributes = set(schema.get_event(event).attributes)
    assert dictation | recording <= attributes


def test_the_browser_file_rejection_is_not_the_server_validation_event(schema: TelemetrySchema) -> None:
    browser = schema.get_event("upload.file_rejected")
    server = schema.get_event("upload.validation_failed")
    assert browser.browser and not server.browser
    assert set(browser.attributes) == {"hmcts.error.code", "file.type", "file.size_bytes"}
    assert "upload.file_count" in server.attributes


def test_dictation_server_events_are_registered(schema: TelemetrySchema) -> None:
    for name in ("document.available", "upload.rejected", "upload.url_issued", "job.finished"):
        assert schema.get_event(name) is not None, name
        assert not schema.get_event(name).browser


def test_browser_events_keep_their_class(schema: TelemetrySchema) -> None:
    browser = {name: e for name, e in schema.events.items() if e.browser}
    assert len(browser) == 49
    assert browser["minutes.rated"].event_class is EventClass.ANALYTICS
    assert browser["speech.unhealthy"].event_class is EventClass.OPERATIONAL


def test_setup_registers_the_schema() -> None:
    from unittest.mock import patch

    from transcribe_api.runtime import telemetry

    with (
        patch.object(telemetry, "setup_telemetry"),
        patch.object(telemetry, "register_schema", wraps=register_schema) as register,
    ):
        telemetry.configure_telemetry()
    register.assert_called_once_with()
    assert default_schema.get_event("upload.file_rejected") is not None


# The contract ---------------------------------------------------------------------------


def test_the_contract_file_is_current(schema: TelemetrySchema) -> None:
    """telemetry-contract.json is the schema as JSON for transcribe-web and the shared
    infrastructure's query checks. Regenerate with UPDATE_TELEMETRY_CONTRACT=1."""
    expected = render_contract(schema)
    if os.environ.get("UPDATE_TELEMETRY_CONTRACT") == "1":
        CONTRACT.write_text(expected)
    assert CONTRACT.is_file(), "telemetry-contract.json is missing: run with UPDATE_TELEMETRY_CONTRACT=1"
    assert CONTRACT.read_text() == expected, (
        "telemetry-contract.json is stale: run "
        "UPDATE_TELEMETRY_CONTRACT=1 pytest tests/unit/test_telemetry_schema.py and commit it"
    )
