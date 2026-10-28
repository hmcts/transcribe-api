"""DIAAT-205: background tasks emit OpenTelemetry spans (they used Sentry transactions,
which stopped recording once Sentry tracing was turned off)."""

from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[4] / "src" / "transcribe_api"


@pytest.mark.parametrize("module", ["documents/minutes/llm_calls.py", "stt/pipeline.py"])
def test_tasks_use_opentelemetry_spans_not_sentry_transactions(module):
    # Read the file: unit-test setup replaces some of these modules with mocks.
    source = (SOURCE / module).read_text()
    assert "start_transaction" not in source
    # Own trace per background task, linked to the originating request.
    assert "background_span(" in source
