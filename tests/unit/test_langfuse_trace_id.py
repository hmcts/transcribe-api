"""Stored trace ids: Langfuse v3+ raises on anything but 32 lowercase hex characters,
and minutes made under the v2 SDK hold UUIDs."""

import pytest

from transcribe_api.documents.llm.llm_client import langfuse_trace_id


@pytest.mark.parametrize("trace_id", ["0af7651916cd43dd8448eb211c80319c"])
def test_otel_trace_ids_are_kept(trace_id: str) -> None:
    assert langfuse_trace_id(trace_id) == trace_id


@pytest.mark.parametrize(
    "trace_id",
    [None, "", "0f8fad5b-d9cb-469f-a165-70867728950e", "0AF7651916CD43DD8448EB211C80319C", "trace-123"],
)
def test_legacy_or_invalid_trace_ids_are_dropped(trace_id: str | None) -> None:
    assert langfuse_trace_id(trace_id) is None

