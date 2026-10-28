"""DIAAT-276 review fix: an edit with no dialogue entries is a client-side rejection."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry

_SOURCE = Path(__file__).resolve().parents[4] / "src" / "transcribe_api" / "documents" / "minutes" / "llm_calls.py"


def _load_llm_calls():
    # Loaded under its own name: other unit-test modules replace transcribe_api.documents.minutes.llm_calls
    # in sys.modules with a mock, which would make a plain import order-dependent.
    spec = importlib.util.spec_from_file_location("llm_calls_under_test", _SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_edit_without_dialogue_is_a_handled_failure():
    llm_calls = _load_llm_calls()
    with capture_telemetry() as telemetry:
        with pytest.raises(HTTPException) as raised:
            await llm_calls.ai_edit_task([], uuid4(), str(uuid4()), "shorter", uuid4(), str(uuid4()))
        assert raised.value.status_code == 400
        (failed,) = telemetry.events("minutes_edit.failed")
        assert failed[c.OUTCOME] == "failure"
        assert failed[c.ERROR_CODE] == "minutes.no_dialogue"
        span = next(s for s in telemetry.finished_spans() if s.name == "minutes_edit")
        assert span.status.status_code.name == "UNSET"


@pytest.mark.asyncio
async def test_edit_of_an_unknown_minute_version_is_a_handled_failure(monkeypatch):
    llm_calls = _load_llm_calls()

    def not_found(*args, **kwargs):
        raise HTTPException(status_code=404, detail="Minute version not found")

    monkeypatch.setattr(llm_calls, "get_minute_version_by_id", not_found)
    with capture_telemetry() as telemetry:
        with pytest.raises(HTTPException):
            await llm_calls.ai_edit_task([object()], uuid4(), str(uuid4()), "shorter", uuid4(), str(uuid4()))
        (failed,) = telemetry.events("minutes_edit.failed")
        assert failed[c.OUTCOME] == "failure"
        assert failed[c.ERROR_CODE] == "http.404"
        span = next(s for s in telemetry.finished_spans() if s.name == "minutes_edit")
        assert span.status.status_code.name == "UNSET"
