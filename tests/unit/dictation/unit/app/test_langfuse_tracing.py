"""Langfuse v5 tracing: stored trace ids, AI edits of minutes and the templates.

Langfuse v3+ raises on any trace id but 32 lowercase hex characters, and minutes made
under the v2 SDK hold UUIDs."""

import importlib
import re
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from transcribe_api.documents.llm.llm_client import langfuse_trace_id
from transcribe_api.domain.models_dictation import DialogueEntry, MinuteVersion

OTEL_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
LEGACY_TRACE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def test_otel_trace_ids_are_kept() -> None:
    assert langfuse_trace_id(OTEL_TRACE_ID) == OTEL_TRACE_ID


@pytest.mark.parametrize("trace_id", [None, "", LEGACY_TRACE_ID, OTEL_TRACE_ID.upper(), "trace-123", "0" * 32])
def test_legacy_or_invalid_trace_ids_are_dropped(trace_id: str | None) -> None:
    assert langfuse_trace_id(trace_id) is None


def _fresh(module: str, monkeypatch):
    # Other test modules stub these in sys.modules; use the real ones here.
    monkeypatch.delitem(sys.modules, module, raising=False)
    return importlib.import_module(module)


def _completion(text: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


@pytest.fixture
def prompt(monkeypatch):
    from transcribe_api.documents.llm.llm_client import langfuse_client

    compiled = MagicMock()
    compiled.compile.return_value = [{"role": "user", "content": "prompt"}]
    monkeypatch.setattr(langfuse_client, "get_prompt", lambda *_a, **_k: compiled)
    return compiled


ENTRIES = [DialogueEntry(speaker="Judge", text="The appeal is allowed.", start_time=0.0, end_time=1.0)]


@pytest.mark.asyncio
async def test_general_style_summary_returns_the_model_output_as_html(prompt, monkeypatch) -> None:
    general_style = _fresh("transcribe_api.documents.minutes.templates.general_style", monkeypatch)
    monkeypatch.setattr(general_style, "llm_completion", AsyncMock(return_value=_completion("## Decision")))

    html = await general_style.generate_general_style_summary(ENTRIES, "user-1")

    assert "<h2>Decision</h2>" in html
    assert "The appeal is allowed." in prompt.compile.call_args.kwargs["meeting_transcript"]


@pytest.mark.asyncio
async def test_crissa_generates_every_section(prompt, monkeypatch) -> None:
    crissa = _fresh("transcribe_api.documents.minutes.templates.crissa", monkeypatch)
    llm = AsyncMock(return_value=_completion("## Section"))
    monkeypatch.setattr(crissa, "llm_completion", llm)

    html = await crissa.generate_full_crissa(ENTRIES, "user-1")

    assert llm.await_count == len(crissa.CRISSA_SECTIONS)
    assert html.count("<h2>Section</h2>") == len(crissa.CRISSA_SECTIONS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "kept"),
    [(OTEL_TRACE_ID, True), (LEGACY_TRACE_ID, False), (None, False)],
)
async def test_ai_edit_traces_to_the_trace_id_it_saves(stored, kept, monkeypatch) -> None:
    llm_calls = _fresh("transcribe_api.documents.minutes.llm_calls", monkeypatch)
    current = MinuteVersion(
        id=uuid.uuid4(),
        transcription_id=uuid.uuid4(),
        html_content="<p>draft</p>",
        template={"name": "General", "description": "d", "category": "c"},
        trace_id=stored,
    )
    saved: list[str | None] = []
    monkeypatch.setattr(llm_calls, "get_minute_version_by_id", lambda *_a: current)
    monkeypatch.setattr(llm_calls, "save_minute_version", lambda minute, **_k: saved.append(minute.trace_id))
    edit = AsyncMock(return_value="<p>edited</p>")
    monkeypatch.setattr(llm_calls, "edit_minutes_with_ai", edit)

    await llm_calls.ai_edit_task(ENTRIES, current.id, uuid.uuid4(), "shorter", current.transcription_id, "user-1")

    traced = edit.await_args.kwargs["langfuse_trace_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", traced)
    assert (traced == stored) is kept
    assert saved == [traced, traced]
