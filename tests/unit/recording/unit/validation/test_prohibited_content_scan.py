"""DIAAT-287: the prohibited-content scan finds what it must, and only that.

These tests use their own capture, not the ``tel`` fixture: what they export is meant
to fail the scan, so it must not be scanned again (and recorded) on teardown.
"""

from __future__ import annotations

import logging

import pytest
from hmcts_otel import get_tracer
from hmcts_otel.redaction import Redactor
from hmcts_otel.testing import capture_telemetry

from tests.unit.recording.unit.validation.prohibited_content import APPELLANT, PATTERNS, SEEDED, scan
from transcribe_api.runtime.telemetry import configure_telemetry

logger = logging.getLogger("transcribe_api.validation")

# One example of each pattern class, built at runtime (no literal in a source line).
EXAMPLES = {
    "email": "someone" + "@" + "example.org",
    "uk_case_reference": "HU" + "/01234/" + "2025",
    "sas_signature": "s" + "ig=" + "aGVsbG8td29ybGQtc2ln",
    "bearer_token": "Bea" + "rer " + "abcdefghijklmnop0123456789",
    "jwt": "ey" + "JhbGciOiJIUzI1NiJ9.ey" + "JzdWIiOiJ4In0.sig",
    "uk_postcode": "M1 " + "1AE",
    "ni_number": "CE" + "123456" + "D",
}


def test_every_pattern_class_has_an_example():
    assert set(EXAMPLES) == set(PATTERNS)
    for name, example in EXAMPLES.items():
        assert PATTERNS[name].search(example), name


def test_a_party_name_in_one_event_fails_the_scan():
    with capture_telemetry() as capture:
        configure_telemetry()
        logger.warning("Job failed for appellant %s", APPELLANT)
        result = scan(capture)
    assert not result.passed
    assert {(f.kind, f.name) for f in result.findings} >= {("seeded", "appellant_name")}
    assert APPELLANT not in result.summary()  # findings never quote the content


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_each_pattern_class_is_found_without_the_services_redaction(name):
    # Redaction off: the scan must not depend on the rules it is checking.
    with capture_telemetry(redactor=Redactor(rules=())) as capture:
        with get_tracer().start_as_current_span("work") as span:
            span.set_attribute("note", "value " + EXAMPLES[name] + " end")
        result = scan(capture, seeded={})
    assert [(f.kind, f.name, f.where) for f in result.findings] == [("pattern", name, "span attribute")]


def test_seeded_values_are_found_wherever_they_are_exported():
    with capture_telemetry(redactor=Redactor(rules=())) as capture:
        with get_tracer().start_as_current_span("work") as span:
            span.add_event("step", {"detail": SEEDED["speaker_name"]})
        logger.error("could not save: %s", SEEDED["transcript_text"])
        result = scan(capture, patterns={})
    found = {(f.name, f.where) for f in result.findings}
    assert ("speaker_name", "span event attribute") in found
    assert ("transcript_text", "log body") in found


def test_identifiers_and_redacted_values_are_not_findings():
    safe = [
        "uploads/00000000-0000-4000-8000-0000000000c1/6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f-[REDACTED]",
        "https://teststorage.blob.core.windows.net/x/y.wav?[REDACTED]",
        "sig=[REDACTED]",
        "Bearer [REDACTED]",
        "trace 1cf63036d0bef260e82c790af1e6588f span b7bedaa7849b3ac7",
        "speech.http_503 storage.db_unavailable upload.unsupported_format",
        "transcription.speech.call.duration",
    ]
    with capture_telemetry(redactor=Redactor(rules=())) as capture:
        for text in safe:
            logger.warning(text)
        result = scan(capture, seeded={})
    assert result.passed, result.summary()


@pytest.mark.parametrize("signal", ["span", "log", "metric"])
def test_a_party_name_in_resource_attributes_fails_the_scan(monkeypatch, signal):
    # Resource attributes are exported with every span, log record and metric; one set
    # through OTEL_RESOURCE_ATTRIBUTES is not redacted, so the scan must look there too.
    from hmcts_otel import record_event

    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"case.party={APPELLANT.replace(' ', '_')}")
    with capture_telemetry(redactor=Redactor(rules=())) as capture:
        if signal == "span":
            with get_tracer().start_as_current_span("work"):
                pass
        elif signal == "log":
            logging.getLogger("transcribe_api.validation").warning("plain")
        else:
            record_event("job.created")  # increments the events counter metric
        surname = APPELLANT.split(" ", 1)[1].replace(" ", "_")
        result = scan(capture, seeded={"appellant_surname": surname})
    wheres = {f.where for f in result.findings if f.kind == "seeded"}
    assert f"{signal} resource attribute" in wheres
