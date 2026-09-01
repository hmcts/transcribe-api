from langfuse.decorators import langfuse_context, observe
from shared_utils.database.postgres_models import DialogueEntry
from uwotm8 import convert_american_to_british_spelling

from transcribe_api.documents.llm.llm_client import (
    langfuse_client,
    llm_completion,
)
from transcribe_api.documents.minutes.templates.utils import format_transcript_string_for_prompt
from transcribe_api.runtime.markdown import markdown_to_html


@observe(name="generate_short_n_sweet_summary", as_type="generation")
async def generate_short_n_sweet_summary(
    dialogue_entries: list[DialogueEntry],
    user_email: str,
) -> str:
    langfuse_context.update_current_trace(
        user_id=user_email,
    )
    prompt = langfuse_client.get_prompt("short-n-sweet-template-prompt", type="chat")
    langfuse_context.update_current_observation(
        prompt=prompt,
        user_id=user_email,
    )

    transcript_string = format_transcript_string_for_prompt(dialogue_entries, include_index=False)
    compiled_chat_prompt = prompt.compile(
        meeting_transcript=transcript_string,
    )
    completion = await llm_completion(
        model="vertex_ai/gemini-2.5-pro-preview-03-25",
        messages=compiled_chat_prompt,
        temperature=0.1,
    )
    markdown_output = completion.choices[0].message.content
    converted_output = convert_american_to_british_spelling(markdown_output)
    html_output = markdown_to_html(converted_output)

    langfuse_context.update_current_observation(
        output=html_output,
    )
    langfuse_context.update_current_trace(
        user_id=user_email,
    )
    return html_output
