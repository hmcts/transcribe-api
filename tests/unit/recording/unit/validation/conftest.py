"""DIAAT-287: recording telemetry validation scenarios and their prohibited-content scan.

Every test in this package that uses ``tel`` has everything it exported scanned on
teardown (tests/unit/telemetry_scan.py, with the recording seeds in
prohibited_content.py): a finding fails the test. The results of every scenario, of
both areas, are written once at the end of the session (tests/unit/conftest.py) to
``test-results/telemetry-pii-scan.json``, which the pipeline archives as evidence.
"""

from __future__ import annotations

import pytest
from hmcts_otel.testing import capture_telemetry

from tests.unit import telemetry_scan
from tests.unit.recording.unit.validation.prohibited_content import SEEDED
from transcribe_api.runtime.telemetry import configure_telemetry


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from transcribe_api.api.routes_recording import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def tel(request):
    """Capture the scenario's telemetry; on teardown, scan all of it (AC3)."""
    with capture_telemetry() as capture:
        configure_telemetry()  # the service schema and redaction rules, as deployed
        yield capture
        result = telemetry_scan.scan(capture, SEEDED)
    telemetry_scan.record_scenario("recording", request.node.nodeid, result, SEEDED)
    if not result.passed:
        pytest.fail(f"prohibited content in exported telemetry: {result.summary()}")
