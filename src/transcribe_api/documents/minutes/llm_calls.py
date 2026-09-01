from uuid import UUID, uuid4

import sentry_sdk
from fastapi import HTTPException
from langfuse.decorators import langfuse_context, observe
from uwotm8 import convert_american_to_british_spelling

from transcribe_api.domain.interface_dictation import (
    create_error_minute_version,
    get_minute_version_by_id,
    save_minute_version,
)
from transcribe_api.domain.models_dictation import (
    DialogueEntry,
    MinuteVersion,
    TemplateMetadata,
    TemplateName,
)
from transcribe_api.documents.llm.llm_client import (
    LLMModel,
    langfuse_client,
    llm_completion,
    structured_output_llm_completion_builder_func,
)
from transcribe_api.documents.minutes.templates.crissa import generate_full_crissa
from transcribe_api.documents.minutes.templates.general_style import generate_general_style_summary
from transcribe_api.documents.minutes.templates.utils import format_transcript_string_for_prompt
from transcribe_api.documents.minutes.types import (
    MeetingTitleOutput,
    SpeakerPredictionOutput,
)
from transcribe_api.runtime.markdown import html_to_markdown, markdown_to_html


@observe(name="edit_minutes_with_ai", as_type="generation")
async def edit_minutes_with_ai(
    current_minute_version: MinuteVersion,
    edit_instructions: str,
    transcript: list[DialogueEntry],
    user_email: str,
    **kwargs,  # noqa: ARG001
) -> str:
    current_markdown_minutes = html_to_markdown(current_minute_version.html_content)
    langfuse_context.update_current_trace(
        user_id=user_email,
    )
    prompt = langfuse_client.get_prompt("ai-edit-prompt", type="chat")
    langfuse_context.update_current_observation(
        prompt=prompt,
    )

    transcript_string = format_transcript_string_for_prompt(transcript, include_index=False)
    compiled_chat_prompt = prompt.compile(
        meeting_transcript=transcript_string,
        meeting_summary=current_markdown_minutes,
        user_instructions=edit_instructions,
    )

    initial_completion = await llm_completion(
        temperature=0.1,
        messages=compiled_chat_prompt,
        model=LLMModel.VERTEX_GEMINI_25_FLASH,
    )
    british_minutes = convert_american_to_british_spelling(initial_completion.choices[0].message.content)
    html_minutes = markdown_to_html(british_minutes)

    return html_minutes


@observe(name="generate_meeting_title", as_type="generation")
async def generate_meeting_title(
    transcript: list[DialogueEntry],
    user_email: str,
) -> str:
    langfuse_context.update_current_trace(
        user_id=user_email,
    )
    prompt = langfuse_client.get_prompt("generate-meeting-title-prompt", type="chat")
    langfuse_context.update_current_observation(
        prompt=prompt,
    )
    transcript_string = format_transcript_string_for_prompt(transcript, include_index=False)
    compiled_chat_prompt = prompt.compile(meeting_transcript=transcript_string)

    meeting_title_completion_func = structured_output_llm_completion_builder_func(MeetingTitleOutput)
    completion = await meeting_title_completion_func(
        messages=compiled_chat_prompt,
        temperature=0.1,
        model=LLMModel.VERTEX_GEMINI_25_FLASH,
    )

    if not completion or not completion.title:
        raise HTTPException(status_code=500, detail="No title generated")

    return completion.title.strip()


@observe(name="generate_speaker_predictions", as_type="generation")
async def generate_speaker_predictions(dialogue_entries: list, user_email: str) -> dict:
    langfuse_context.update_current_trace(
        user_id=user_email,
    )
    prompt = langfuse_client.get_prompt("predict-speaker-names", type="chat")
    langfuse_context.update_current_observation(
        prompt=prompt,
    )
    # Prepare the conversation context
    conversation_context = "\n".join([f"{entry.speaker}: {entry.text}" for entry in dialogue_entries])

    compiled_chat_prompt = prompt.compile(meeting_transcript=conversation_context)
    speaker_prediction_completion_func = structured_output_llm_completion_builder_func(SpeakerPredictionOutput)
    completion = await speaker_prediction_completion_func(
        messages=compiled_chat_prompt,
        temperature=0.1,
        model=LLMModel.VERTEX_GEMINI_25_FLASH,
    )

    predictions = completion

    if not predictions:
        raise HTTPException(status_code=500, detail="No predictions found")

    return {pred.original_speaker: pred.predicted_name for pred in predictions.predictions}


@observe(name="generate_summary_task", as_type="generation")
async def generate_llm_output_task(
    dialogue_entries: list[DialogueEntry],
    transcription_id: UUID,
    template: TemplateMetadata,
    user_email: str,
    minute_version_id: UUID | None = None,
) -> str:
    # Start a Sentry transaction for the whole function
    with sentry_sdk.start_transaction(op="task", name="Generate LLM Output Task") as transaction:  # noqa: F841
        # Ensure we have a consistent ID through the whole process
        if minute_version_id is None:
            minute_version_id = uuid4()  # Generate a UUID if none provided

        # save initial version with is_generating=True
        initial_minute_version = MinuteVersion(
            id=minute_version_id,
            transcription_id=transcription_id,
            html_content="",
            template=template,
            trace_id=langfuse_context.get_current_trace_id(),
            is_generating=True,
        )
        save_minute_version(initial_minute_version)

        try:
            # Generate content based on template
            if template.name == TemplateName.GENERAL:
                llm_output = await generate_general_style_summary(dialogue_entries, user_email)
            elif template.name == TemplateName.CRISSA:
                llm_output = await generate_full_crissa(dialogue_entries, user_email)
            else:
                msg = "Invalid template parameter"
                raise ValueError(msg)

            # Update the existing minute_version instead of creating a new one
            initial_minute_version.html_content = llm_output
            initial_minute_version.is_generating = False
            save_minute_version(initial_minute_version)

            return llm_output  # noqa: TRY300

        except Exception as e:
            # Save error state
            error_minute_version = create_error_minute_version(
                minute_version_id,
                transcription_id,
                e,
                template=template,
                trace_id=langfuse_context.get_current_trace_id(),
            )
            save_minute_version(error_minute_version)
            raise


async def ai_edit_task(
    dialogue_entries: list[DialogueEntry],
    current_minute_version_id: UUID,
    new_minute_version_id: UUID,
    edit_instructions: str,
    transcription_id: UUID,
    user_email: str,
) -> str:
    with sentry_sdk.start_transaction(op="task", name="AI Edit Task") as transaction:  # noqa: F841
        # if length of dialogue entries is 0, return empty string
        if len(dialogue_entries) == 0:
            raise HTTPException(status_code=400, detail="No dialogue entries found")

        current_minutes = get_minute_version_by_id(current_minute_version_id, transcription_id)

        new_minute_version = MinuteVersion(
            id=new_minute_version_id,
            transcription_id=transcription_id,
            html_content="",
            template=current_minutes.template,
            trace_id=current_minutes.trace_id,
            is_generating=True,
        )
        save_minute_version(new_minute_version)
        try:
            llm_output = await edit_minutes_with_ai(
                current_minutes,
                edit_instructions,
                dialogue_entries,
                user_email,
                langfuse_parent_trace_id=current_minutes.trace_id,
            )

            new_minute_version.html_content = llm_output
            new_minute_version.is_generating = False
            save_minute_version(new_minute_version)

            return llm_output  # noqa: TRY300

        except Exception as e:
            # Pass template from current_minutes to create valid error state
            error_minute_version = create_error_minute_version(
                new_minute_version_id,
                transcription_id,
                e,
                template=current_minutes.template
                if isinstance(current_minutes.template, dict)
                else current_minutes.template.model_dump(),
            )
            save_minute_version(error_minute_version)
            raise


@observe(name="generate_realtime_summary", as_type="generation")
async def generate_realtime_summary(
    transcript_entries: list[dict],
    existing_summary: str | None = None,
    **kwargs,  # noqa: ARG001
) -> str:
    """
    Generate a real-time summary of conversation transcript entries.

    This function takes transcript entries from real-time speech recognition
    and generates an AI summary. It can update an existing summary with new
    entries or create a fresh summary.

    Args:
        transcript_entries: List of transcript entries with speaker, text, and timestamp
        existing_summary: Optional existing summary to update with new entries

    Returns:
        str: Generated or updated summary in markdown format
    """
    # Format the transcript entries for the prompt
    transcript_text = "\n\n".join(
        [f"**{entry['speaker']}** ({entry['timestamp']}): {entry['text']}" for entry in transcript_entries]
    )

    # Build the prompt based on whether we're updating or creating new
    if existing_summary:
        system_prompt = """You are an AI assistant that generates concise, structured summaries of ongoing conversations.
Your task is to update an existing summary with new transcript entries.

Guidelines:
- Maintain the existing structure and key points
- Integrate new information seamlessly
- Keep the summary concise and well-organized
- Use markdown formatting with headers and bullet points
- Focus on key points, decisions, and action items
- Update the summary to reflect the latest state of the conversation"""

        user_prompt = f"""Existing Summary:
{existing_summary}

New Transcript Entries:
{transcript_text}

Please update the summary to incorporate the new information while maintaining clarity and structure."""
    else:
        system_prompt = """You are an AI assistant that generates concise, structured summaries of conversations.
Your task is to create a clear, organized summary of the transcript provided.

Guidelines:
- Use markdown formatting with headers and bullet points
- Structure the summary logically (e.g., Key Points, Decisions, Action Items)
- Be concise but capture all important information
- Focus on substance over verbatim transcription
- Identify key themes and topics discussed"""

        user_prompt = f"""Transcript:
{transcript_text}

Please generate a structured summary of this conversation."""

    try:
        # Call the LLM to generate the summary
        response = await llm_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=LLMModel.GPT_4O_MINI,
            temperature=0.3,
        )
    except Exception as e:
        sentry_sdk.capture_exception(e)
        raise HTTPException(status_code=500, detail=f"Failed to generate realtime summary: {e!s}") from e
    else:
        summary = response.strip()

        langfuse_context.update_current_observation(
            input={
                "transcript_entries": transcript_entries,
                "existing_summary": existing_summary,
            },
            output=summary,
            metadata={
                "num_entries": len(transcript_entries),
                "is_update": existing_summary is not None,
            },
        )

        return summary
