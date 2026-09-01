# ruff: noqa

from datetime import UTC, datetime

from langfuse.decorators import langfuse_context, observe
from pydantic import BaseModel
from uwotm8 import convert_american_to_british_spelling

from transcribe_api.domain.models_dictation import DialogueEntry, TemplateName
from transcribe_api.documents.llm.llm_client import (
    LLMModel,
    langfuse_client,
    llm_completion,
    structured_output_llm_completion_builder_func,
)
from transcribe_api.documents.minutes.templates.utils import format_transcript_string_for_prompt
from transcribe_api.runtime.markdown import html_to_markdown, markdown_to_html

CRISSA_SECTIONS = [
    "Check in",
    "Review",
    "Intervention",
    "Summary",
    "Set task",
    "Appointment",
]


@observe(name="generate_crissa_section", as_type="generation")
async def generate_crissa_section(
    transcript_string: str,
    previous_outputs: list[str],
    user_email: str,
    section: str,
    today_date_readable: str,
    model_name: str,
    prompt_version: int | None = None,
    temperature: float = 0.1,
) -> tuple[str, str]:
    trace_id = langfuse_context.get_current_trace_id()

    langfuse_context.update_current_trace(
        user_id=user_email,
    )

    prompt = langfuse_client.get_prompt("crissa-prompt", version=prompt_version, type="chat")
    langfuse_context.update_current_observation(
        prompt=prompt,
        user_id=user_email,
    )

    compiled_chat_prompt = prompt.compile(
        meeting_transcript=transcript_string,
        today_date_readable=today_date_readable,
        crissa_draft_sections="\n\n".join(previous_outputs),
        section=section,
    )

    completion = await llm_completion(
        temperature=temperature,
        messages=compiled_chat_prompt,
        model=model_name,
    )

    markdown_output = completion.choices[0].message.content

    markdown_output = convert_american_to_british_spelling(markdown_output)
    html_output = markdown_to_html(markdown_output)

    return html_output, trace_id


@observe(name="generate_full_crissa", as_type="generation")
async def generate_full_crissa(
    dialogue_entries: list[DialogueEntry],
    user_email: str,
    prompt_version: int | None = None,
    model_name: str = LLMModel.VERTEX_GEMINI_25_PRO,
    temperature: float = 0.1,
) -> str:
    all_sections: list[str] = []
    trace_id: str | None = None

    transcript_string = format_transcript_string_for_prompt(dialogue_entries, include_index=False)

    today_date_readable = datetime.now(UTC).strftime("%d %B %Y")

    # Generate all sections sequentially
    for section in CRISSA_SECTIONS:
        section_output, current_trace_id = await generate_crissa_section(
            transcript_string=transcript_string,
            previous_outputs=all_sections,  # Pass all previously generated sections
            user_email=user_email,
            section=section,
            today_date_readable=today_date_readable,
            prompt_version=prompt_version,
            model_name=model_name,
            temperature=temperature,
        )
        all_sections.append(section_output)
        if trace_id is None:
            trace_id = current_trace_id

    # Combine all sections
    combined_html = "\n\n".join(all_sections)

    langfuse_context.update_current_observation(
        user_id=user_email,
        input={"template": TemplateName.CRISSA, "dialogue_entries": transcript_string},
        output=html_to_markdown(combined_html),
    )
    langfuse_context.update_current_trace(
        user_id=user_email,
        input={"template": TemplateName.CRISSA, "dialogue_entries": transcript_string},
        output=html_to_markdown(combined_html),
    )

    return combined_html
