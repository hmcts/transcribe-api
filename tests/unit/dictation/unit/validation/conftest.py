"""DIAAT-287: every dictation validation scenario's telemetry is scanned for prohibited
content.

Tests in this folder take the ``telemetry`` fixture (a ``capture_telemetry`` capture,
opened before the app is instrumented). When the test finishes, everything it captured
is scanned (tests/unit/telemetry_scan.py, with the dictation seeds in pii_scan.py): a
finding fails that test, naming where and which check, never the matched text. The
results of every scenario, of both areas, are written once at the end of the session
(tests/unit/conftest.py) to ``test-results/telemetry-pii-scan.json``, which the
pipeline archives as evidence.
"""

from __future__ import annotations

from typing import Any

import pytest
from hmcts_otel.testing import capture_telemetry

from tests.unit import telemetry_scan
from tests.unit.dictation.unit.validation import pii_scan


@pytest.fixture
def scenario_notes() -> dict[str, Any]:
    """Extra, value-free facts a scenario records in the evidence (e.g. the dashboard
    values its events should produce)."""
    return {}


@pytest.fixture
def telemetry(request, scenario_notes):
    """A telemetry capture whose content is scanned when the test ends."""
    with capture_telemetry() as capture:
        yield capture
        result = telemetry_scan.scan(capture, pii_scan.SEEDED)
        captured_events = bool(capture.events())
        telemetry_scan.record_scenario("dictation", request.node.nodeid, result, pii_scan.SEEDED, scenario_notes)
    assert captured_events, "the scenario captured no events: the scan would be vacuous"
    assert result.passed, f"prohibited content in exported telemetry: {result.summary()}"
