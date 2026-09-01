from datetime import datetime
from zoneinfo import ZoneInfo

from langfuse.decorators import langfuse_context, observe
from uwotm8 import convert_american_to_british_spelling

from transcribe_api.domain.models_dictation import DialogueEntry, TemplateName
from transcribe_api.documents.llm.llm_client import (
    LLMModel,
    langfuse_client,
    llm_completion,
)
from transcribe_api.documents.minutes.templates.utils import format_transcript_string_for_prompt
from transcribe_api.runtime.markdown import markdown_to_html


@observe(name="generate_general_style_summary", as_type="generation")
async def generate_general_style_summary(
    dialogue_entries: list[DialogueEntry],
    user_email: str,
    prompt_name: str = "general-style-template-prompt",
    prompt_version: int | None = None,
    model_name: str = LLMModel.VERTEX_GEMINI_25_PRO,
    temperature: float = 0.1,
) -> str:
    transcript_string = format_transcript_string_for_prompt(dialogue_entries, include_index=False)
    prompt = langfuse_client.get_prompt(prompt_name, version=prompt_version, type="chat")

    langfuse_context.update_current_observation(
        prompt=prompt,
        user_id=user_email,
        input={"template": TemplateName.GENERAL, "dialogue_entries": transcript_string},
    )

    compiled_chat_prompt = prompt.compile(
        meeting_transcript=transcript_string,
        date=datetime.now(tz=ZoneInfo("Europe/London")).strftime("%d %B %Y"),
    )

    completion = await llm_completion(
        temperature=temperature,
        messages=compiled_chat_prompt,
        model=model_name,
    )
    output = completion.choices[0].message.content

    converted_output = convert_american_to_british_spelling(output)

    langfuse_context.update_current_observation(
        output=output,
    )
    langfuse_context.update_current_trace(
        user_id=user_email,
        input={"template": TemplateName.GENERAL, "dialogue_entries": transcript_string},
        output=output,
    )
    html_content = markdown_to_html(converted_output)

    return html_content
