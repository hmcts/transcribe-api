"""DIAAT-287: the prohibited-content scan finds what it must, where it must, and
does not flag ordinary telemetry (so a clean result means something)."""

import logging

import pytest
from hmcts_otel._state import STATE
from hmcts_otel.testing import capture_telemetry

from tests.unit.dictation.unit.validation import pii_scan

PATTERN_SAMPLES = {
    "email": pii_scan.APPELLANT_EMAIL,
    "uk_case_reference": pii_scan.CASE_REFERENCE,
    "sas_signature": pii_scan.SAS_URL,
    "bearer_token": f"Bearer {pii_scan.BEARER_TOKEN}",
    "jwt": pii_scan.BEARER_TOKEN,
    "uk_postcode": pii_scan.POSTCODE,
    "ni_number": pii_scan.NI_NUMBER,
}

# Strings real telemetry is made of: none of them is prohibited.
BENIGN = [
    "3f2b8c1a-9d4e-4f6a-8b7c-1d2e3f4a5b6c",
    "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f",
    "0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F",
    "dictation.session_ended",
    "speech.canceled Error AuthenticationFailure",
    "hmcts.journey.parent_id",
    "2026-01-15T10:00:00+00:00",
    "GET /api/download",
    "/api/transcriptions/{transcription_id}/document-download",
    "user-uploads/[redacted]/hearing-document-6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f.docx",
    "https://teststorage.blob.core.windows.net/c/doc.docx?[REDACTED]",
    "sig=[REDACTED]",
    "Authorization: Bearer [REDACTED]",
    "Role check passed for user [redacted] (roles=['Judge'])",
    "document.template_version=4f1c2d3e4b5a",
    "http.500",
    "Python/3.13 testclient",
]


@pytest.mark.parametrize("name", sorted(pii_scan.PATTERNS))
def test_every_pattern_class_matches_its_sample(name):
    assert set(PATTERN_SAMPLES) == set(pii_scan.PATTERNS)
    assert pii_scan.PATTERNS[name].search(PATTERN_SAMPLES[name])


def test_every_seeded_value_is_found_by_value():
    for label, value in pii_scan.SEEDED.items():
        found = pii_scan.scan_items([pii_scan.Exported("log attribute", f"before {value} after")], pii_scan.SEEDED)
        assert ("seeded", label) in {(f.kind, f.name) for f in found}


def test_ordinary_telemetry_is_not_flagged():
    items = [pii_scan.Exported("span attribute", text) for text in BENIGN]
    assert pii_scan.scan_items(items, pii_scan.SEEDED) == []


def test_the_scan_reads_every_kind_of_exported_item():
    """Span names, attributes, span events, log bodies and metric attributes (custom
    events are log records: the e2e scenarios have them): a case detail in any of them is a finding."""
    with capture_telemetry() as telemetry:
        tracer = STATE.tracer_provider.get_tracer(__name__)
        with tracer.start_as_current_span(f"render {pii_scan.PII_APPELLANT}") as span:
            span.set_attribute("party", pii_scan.PII_RESPONDENT)
            span.add_event("note", {"text": pii_scan.POSTCODE})
        logging.getLogger("app.validation").warning("submitted for %s", pii_scan.PII_JUDGE)
        meter = STATE.meter_provider.get_meter(__name__)
        meter.create_counter("validation.counter").add(1, {"who": pii_scan.NI_NUMBER})
        found = {(f.where, f.kind, f.name) for f in pii_scan.scan(telemetry, pii_scan.SEEDED).findings}
    assert {
        ("span name", "seeded", "appellant_name"),
        ("span attribute", "seeded", "respondent_name"),
        ("span event attribute", "pattern", "uk_postcode"),
        ("log body", "seeded", "judge_name"),
        ("metric attribute", "pattern", "ni_number"),
    } <= found


def test_a_finding_never_carries_the_matched_text():
    found = pii_scan.scan_items(
        [pii_scan.Exported("log body", f"{pii_scan.PII_APPELLANT} {pii_scan.CASE_REFERENCE}")], pii_scan.SEEDED
    )
    assert found
    text = repr([f.as_dict() for f in found])
    assert pii_scan.PII_APPELLANT not in text
    assert pii_scan.CASE_REFERENCE not in text
