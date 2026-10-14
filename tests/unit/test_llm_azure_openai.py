"""Azure OpenAI: the model name matches the gpt-4o deployment in
transcribe-shared-infrastructure/openai.tf, auth is the workload identity (no
API key), and the realtime summary calls the model with valid arguments.
"""

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import litellm
import pytest

from transcribe_api.documents.llm.llm_client import LLMModel

_LLM_CALLS = "transcribe_api.documents.minutes.llm_calls"


@pytest.fixture
def llm_calls(monkeypatch):
    # Other test modules stub llm_calls in sys.modules; use the real one here.
    monkeypatch.delitem(sys.modules, _LLM_CALLS, raising=False)
    return importlib.import_module(_LLM_CALLS)


def test_gpt4o_targets_the_gpt4o_deployment() -> None:
    assert LLMModel.AZURE_GPT_4O == "azure/gpt-4o"


def test_litellm_uses_managed_identity_for_azure() -> None:
    assert litellm.enable_azure_ad_token_refresh is True


@pytest.mark.asyncio
async def test_realtime_summary_sends_messages_to_gpt4o(llm_calls) -> None:
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="  ## Summary  "))])
    with patch.object(llm_calls, "llm_completion", AsyncMock(return_value=completion)) as llm:
        summary = await llm_calls.generate_realtime_summary(
            transcript_entries=[{"speaker": "Judge", "timestamp": "00:01", "text": "Hello"}]
        )

    assert summary == "## Summary"
    kwargs = llm.await_args.kwargs
    assert kwargs["model"] == LLMModel.AZURE_GPT_4O
    assert [m["role"] for m in kwargs["messages"]] == ["system", "user"]
    assert "**Judge** (00:01): Hello" in kwargs["messages"][1]["content"]
