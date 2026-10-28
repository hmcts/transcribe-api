"""DIAAT-205: background tasks emit OpenTelemetry spans (they used Sentry transactions,
which stopped recording once Sentry tracing was turned off)."""

from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[4] / "src" / "transcribe_api"


def _source(module):
    # Read the file: unit-test setup replaces some of these modules with mocks.
    return (SOURCE / module).read_text()


@pytest.mark.parametrize("module", ["documents/minutes/llm_calls.py", "stt/pipeline.py"])
def test_no_sentry_transactions(module):
    assert "start_transaction" not in _source(module)


def test_background_traces_start_only_at_task_boundaries():
    """A new trace belongs where work is detached (create_task, the poller's job), not
    in shared task functions: called from the pipeline, minutes must stay in its trace."""
    assert "background_span(" not in _source("documents/minutes/llm_calls.py")
    assert "background_span(" in _source("api/routes_dictation.py")  # the minutes create_task
    assert "background_span(" in _source("stt/pipeline.py")  # the polled job
