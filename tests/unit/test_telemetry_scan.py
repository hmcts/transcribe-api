"""DIAAT-287: the shared prohibited-content scan engine (tests/unit/telemetry_scan.py).

The merged pattern classes must catch everything either area's scanner caught, and the
RE2 subset (what the deployed KQL sample scan runs) must stand on its own.
"""

import pytest

from tests.unit import telemetry_scan
from tests.unit.telemetry_scan import PATTERNS, RE2_PATTERNS, Counts, Exported, ScanResult

# One sample per pattern class, matched by both the union and the RE2 subset.
SAMPLES = {
    "email": "clerk.one@justice.example",
    "uk_case_reference": "PA/50123/2024",
    "sas_signature": "?sv=2024-05-04&sig=c2VjcmV0LXNpZ25hdHVyZQ%3D%3D",
    # Synthetic tokens, built at runtime so no secret scanner mistakes them for real ones.
    "bearer_token": "Bearer " + "eyJ" + "hbGciOiJSUzI1NiJ9" + ".payload.signature",
    "jwt": "eyJ" + "hbGciOiJSUzI1NiJ9" + "." + "eyJ" + "zdWIiOiJjbGVyayJ9" + "." + "c2lnbmF0dXJl",
    "uk_postcode": "SW1A 2AA",
    "ni_number": "AB 12 34 56 C",
}

# Each area's own case-reference forms: none may be lost by the merge.
CASE_REFERENCES = [
    "PA/50123/2024",  # IAC appeal number
    "HU-01234-2023",
    "UI-2024-001234",  # Upper Tribunal
    "PA_50123_2024.mp3",  # in a file name
    "hearing_PA_50123_2024_part1.mp3",  # next to other words in a file name
    "pa_50123_2024 smith",  # lower case
    "T20247001",  # Crown Court
    "ZW24C50012",  # county / family court
]


def test_every_class_is_in_both_sets():
    assert set(PATTERNS) == set(RE2_PATTERNS) == set(SAMPLES)


@pytest.mark.parametrize("name", sorted(SAMPLES))
@pytest.mark.parametrize("patterns", [PATTERNS, RE2_PATTERNS], ids=["union", "re2"])
def test_each_set_matches_every_sample_on_its_own(patterns, name):
    assert patterns[name].search(SAMPLES[name])


@pytest.mark.parametrize("text", CASE_REFERENCES)
@pytest.mark.parametrize("patterns", [PATTERNS, RE2_PATTERNS], ids=["union", "re2"])
def test_every_case_reference_form_of_either_area_is_caught(patterns, text):
    assert patterns["uk_case_reference"].search(text)


def test_the_re2_subset_has_no_lookarounds():
    for pattern in RE2_PATTERNS.values():
        assert not any(token in pattern.pattern for token in ("(?=", "(?!", "(?<=", "(?<!"))


def test_a_finding_names_the_attribute_never_the_value():
    items = [Exported("log attribute", "contact clerk.one@justice.example", "user.contact")]
    (finding,) = telemetry_scan.scan_items(items, {})
    assert finding.as_dict() == {"kind": "pattern", "name": "email", "where": "log attribute", "key": "user.contact"}


def _result(events: int, findings=()) -> ScanResult:
    return ScanResult(Counts(custom_events=events), list(findings))


def test_a_vacuous_or_failed_session_never_passes_the_evidence(monkeypatch):
    monkeypatch.setattr(telemetry_scan, "SCENARIOS", [])
    monkeypatch.setattr(telemetry_scan, "SEEDED_LABELS", {})
    telemetry_scan.record_scenario("dictation", "a", _result(events=3), {"x": "y"}, {"result": "pass"})
    assert telemetry_scan.evidence()["result"] == "pass"
    assert telemetry_scan.SCENARIOS[0]["notes"] == {"result": "pass"}  # notes cannot replace the result
    assert telemetry_scan.evidence(exit_status=1)["result"] == "fail"  # the session failed

    telemetry_scan.record_scenario("recording", "b", _result(events=0), {"x": "y"})
    report = telemetry_scan.evidence()
    assert (report["result"], report["scenarios_failed"]) == ("fail", ["b"])  # captured nothing
    assert report["areas_scanned"] == ["dictation", "recording"]
