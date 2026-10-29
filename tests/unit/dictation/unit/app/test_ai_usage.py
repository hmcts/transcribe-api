"""OBS-5: LLM token usage is metered by feature, provider, model and fallback.

The real code path runs (generate_meeting_title -> llm_completion -> the fallback
chain -> litellm.acompletion) with litellm's own ``mock_response``, so litellm's
logging and the hmcts-otel callback run as in production; only the provider HTTP
call is replaced. Nothing here sets a price: usage only.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from unittest.mock import MagicMock
from uuid import uuid4

import litellm
import pytest
from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry

# The application modules, imported per test (below): the LLM client reads its settings
# at import, so it is imported under the test environment, not at collection.
llm_client = llm_calls = LLMModel = general_template = configure_telemetry = None


@pytest.fixture(autouse=True)
def _application(setup_test_environment):  # the env the client imports with
    global llm_client, llm_calls, LLMModel, general_template, configure_telemetry  # noqa: PLW0603
    # Other test modules stub transcribe_api.documents.minutes.llm_calls with a MagicMock: this needs the real one.
    if not isinstance(sys.modules.get("transcribe_api.documents.minutes.llm_calls", ModuleType("")), ModuleType):
        del sys.modules["transcribe_api.documents.minutes.llm_calls"]
    from transcribe_api.documents.llm import llm_client
    from transcribe_api.documents.llm.llm_client import LLMModel
    from transcribe_api.documents.minutes import llm_calls
    from transcribe_api.documents.minutes.templates.templates_metadata import general_template
    from transcribe_api.runtime.telemetry import configure_telemetry


PROMPT_SENTINEL = "OBS5-PROMPT-SENTINEL The appellant said PA/50123/2024 at the hearing"
RESPONSE_SENTINEL = "OBS5-RESPONSE-SENTINEL judge@justice.gov.uk"
CONTENT_FILTER = "HARM_CATEGORY_SEXUALLY_EXPLICIT: response blocked"


def _title(text: str = RESPONSE_SENTINEL) -> str:
    return '{"title": "%s"}' % text  # noqa: UP031 - JSON with a literal


@pytest.fixture
def litellm_mock(monkeypatch):
    """Answer each model from a table, through litellm's mock_response.

    Patches both names the code calls: llm_client's imported ``acompletion`` and
    ``litellm.acompletion`` (litellm's SDK fallbacks call the latter). Each call still
    goes through the real litellm.acompletion: its logging and callbacks run.
    """
    real = litellm.acompletion
    answers: dict[str, object] = {}
    calls: list[str] = []

    async def fake(*args, **kwargs):
        model = str(kwargs["model"])
        calls.append(model)
        kwargs["mock_response"] = answers[model]
        kwargs.pop("max_retries", None)  # no client retries against a mock
        kwargs.pop("num_retries", None)
        return await real(*args, **kwargs)

    monkeypatch.setattr(llm_client, "acompletion", fake)
    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setitem(llm_client.llm_params, "VERTEX_LOCATIONS", ["europe-west1"])
    monkeypatch.setattr(llm_client, "_ensure_langfuse_authenticated", lambda: None)
    prompt = MagicMock()
    prompt.compile.return_value = [{"role": "user", "content": PROMPT_SENTINEL}]
    monkeypatch.setattr(llm_calls.langfuse_client, "get_prompt", lambda *_a, **_k: prompt)
    return answers, calls


@pytest.fixture
def telemetry():
    configure_telemetry()  # registers the litellm callback, as at startup
    with capture_telemetry() as capture:
        yield capture


async def _logged():
    """Wait for litellm's logging worker: callbacks run there, after the call returns
    (in the caller's context, so the feature and fallback flag still apply)."""
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    for _ in range(5):  # the call schedules its logging as a task: let it enqueue
        await asyncio.sleep(0)
    await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=2)


def _requests(telemetry):
    return [(p["attributes"], p["value"]) for p in telemetry.metric_points(c.METRIC_AI_REQUESTS)]


def _tokens(telemetry):
    return {
        (
            p["attributes"][c.GEN_AI_REQUEST_MODEL],
            p["attributes"][c.GEN_AI_TOKEN_TYPE],
            p["attributes"][c.AI_FALLBACK],
            p["attributes"][c.OUTCOME],
        ): p["sum"]
        for p in telemetry.metric_points(c.METRIC_GEN_AI_TOKEN_USAGE)
    }


async def _generate_title():
    title = await llm_calls.generate_meeting_title([], "user-1")
    await _logged()
    return title


def test_configure_telemetry_registers_the_usage_callback_once():
    configure_telemetry()
    configure_telemetry()
    ours = [cb for cb in litellm.callbacks if getattr(cb, "hmcts_otel_usage", False) is True]
    assert len(ours) == 1


@pytest.mark.asyncio
async def test_gemini_success_is_recorded_with_feature_provider_and_model(litellm_mock, telemetry):
    answers, _ = litellm_mock
    answers[LLMModel.VERTEX_GEMINI_25_FLASH] = _title()

    assert await _generate_title() == RESPONSE_SENTINEL

    ((attributes, count),) = _requests(telemetry)
    assert count == 1
    assert attributes[c.AI_FEATURE] == "title_generation"
    assert attributes[c.GEN_AI_PROVIDER_NAME] == "gcp.vertex_ai"
    assert attributes[c.GEN_AI_REQUEST_MODEL] == "gemini-2.5-flash"
    assert attributes[c.OUTCOME] == "success"
    assert attributes[c.AI_FALLBACK] is False
    tokens = _tokens(telemetry)
    assert tokens[("gemini-2.5-flash", "input", False, "success")] > 0
    assert tokens[("gemini-2.5-flash", "output", False, "success")] > 0


@pytest.mark.asyncio
async def test_gemini_content_filter_falls_back_to_grok_marked_as_fallback(litellm_mock, telemetry):
    answers, calls = litellm_mock
    answers[LLMModel.VERTEX_GEMINI_25_FLASH] = Exception(CONTENT_FILTER)
    answers[LLMModel.VERTEX_GEMINI_20_FLASH] = Exception(CONTENT_FILTER)
    answers[LLMModel.AZURE_GROK_3] = _title()

    assert await _generate_title() == RESPONSE_SENTINEL

    assert calls[-1] == LLMModel.AZURE_GROK_3
    by_model = {(a[c.GEN_AI_REQUEST_MODEL], a[c.OUTCOME]): a for a, _ in _requests(telemetry)}
    gemini = by_model[("gemini-2.5-flash", "error")]
    assert gemini[c.AI_FALLBACK] is False
    assert gemini[c.ERROR_TYPE] == "InternalServerError"  # the class, never the message
    grok = by_model[("grok-3", "success")]
    assert grok[c.AI_FALLBACK] is True
    assert grok[c.GEN_AI_PROVIDER_NAME] == "azure.ai.openai"
    assert grok[c.AI_FEATURE] == "title_generation"
    tokens = _tokens(telemetry)
    assert tokens[("grok-3", "input", True, "success")] > 0
    assert tokens[("gemini-2.5-flash", "input", False, "error")] == 0


@pytest.mark.asyncio
async def test_grok_failure_falls_back_to_gpt4o_marked_as_fallback(litellm_mock, telemetry):
    answers, _ = litellm_mock
    answers[LLMModel.VERTEX_GEMINI_25_FLASH] = Exception(CONTENT_FILTER)
    answers[LLMModel.VERTEX_GEMINI_20_FLASH] = Exception(CONTENT_FILTER)
    answers[LLMModel.AZURE_GROK_3] = Exception("grok unavailable")
    answers[LLMModel.AZURE_GPT_4O] = _title()

    await _generate_title()

    by_model = {(a[c.GEN_AI_REQUEST_MODEL], a[c.OUTCOME]): a for a, _ in _requests(telemetry)}
    assert by_model[("grok-3", "error")][c.AI_FALLBACK] is True
    assert by_model[("gpt-4o", "success")][c.AI_FALLBACK] is True  # CNP's deployment, azure/gpt-4o


@pytest.mark.asyncio
async def test_gemini_model_fallback_is_marked_but_a_same_model_retry_is_not(litellm_mock, telemetry):
    # litellm's SDK fallbacks (another Gemini model) are invisible to callbacks unless
    # marked: the smaller model answering is a fallback.
    answers, _ = litellm_mock
    answers[LLMModel.VERTEX_GEMINI_25_FLASH] = Exception("Vertex unavailable")
    answers[LLMModel.VERTEX_GEMINI_20_FLASH] = _title()

    await _generate_title()

    by_model = {(a[c.GEN_AI_REQUEST_MODEL], a[c.OUTCOME]): a for a, _ in _requests(telemetry)}
    assert by_model[("gemini-2.0-flash", "success")][c.AI_FALLBACK] is True
    assert by_model[("gemini-2.5-flash", "error")][c.AI_FALLBACK] is False


def test_gemini_model_fallbacks_mark_only_other_models():
    fallbacks = llm_client._gemini_model_fallbacks(LLMModel.VERTEX_GEMINI_25_FLASH, {"trace": "x"})
    assert fallbacks[0] == LLMModel.VERTEX_GEMINI_25_FLASH  # a retry of the same model
    assert fallbacks[1] == {
        "model": LLMModel.VERTEX_GEMINI_20_FLASH,
        "metadata": {"trace": "x", "hmcts_ai_fallback": True},
    }
    pro = llm_client._gemini_model_fallbacks(LLMModel.VERTEX_GEMINI_25_PRO, None)
    assert all(isinstance(f, dict) and f["metadata"]["hmcts_ai_fallback"] is True for f in pro)


@pytest.mark.parametrize(
    ("call", "feature"),
    [
        ("edit", "minutes_ai_edit"),
        ("speakers", "speaker_naming"),
    ],
)
@pytest.mark.asyncio
async def test_each_llm_call_site_names_its_feature(litellm_mock, telemetry, call, feature):
    answers, _ = litellm_mock
    if call == "edit":
        answers[LLMModel.VERTEX_GEMINI_25_FLASH] = "Minutes " + RESPONSE_SENTINEL
        version = MagicMock(html_content="<p>minutes</p>")
        await llm_calls.edit_minutes_with_ai(version, "shorter", [], "user-1")
    else:
        answers[LLMModel.VERTEX_GEMINI_25_FLASH] = '{"predictions": []}'
        await llm_calls.generate_speaker_predictions([], "user-1")
    await _logged()

    assert {a[c.AI_FEATURE] for a, _ in _requests(telemetry)} == {feature}


@pytest.mark.asyncio
async def test_minutes_generation_is_one_feature_for_every_template(litellm_mock, telemetry, monkeypatch):
    answers, _ = litellm_mock
    answers[LLMModel.VERTEX_GEMINI_25_PRO] = "Minutes " + RESPONSE_SENTINEL
    monkeypatch.setattr(llm_calls, "save_minute_version", lambda *_a, **_k: None)

    async def general(*_args):
        completion = await llm_client.llm_completion(model=LLMModel.VERTEX_GEMINI_25_PRO, messages=[])
        return completion.choices[0].message.content

    monkeypatch.setattr(llm_calls, "generate_general_style_summary", general)
    await llm_calls.generate_llm_output_task([], uuid4(), general_template, "user-1")
    await _logged()

    assert {a[c.AI_FEATURE] for a, _ in _requests(telemetry)} == {"minutes_generation"}


@pytest.mark.asyncio
async def test_no_prompt_or_response_content_is_exported(litellm_mock, telemetry):
    answers, _ = litellm_mock
    answers[LLMModel.VERTEX_GEMINI_25_FLASH] = Exception(CONTENT_FILTER)
    answers[LLMModel.VERTEX_GEMINI_20_FLASH] = Exception(CONTENT_FILTER)
    answers[LLMModel.AZURE_GROK_3] = _title()

    await _generate_title()

    assert _requests(telemetry)  # something was recorded
    telemetry.assert_no_prohibited_content(PROMPT_SENTINEL, RESPONSE_SENTINEL, "OBS5-")
