import json
import tempfile
from pathlib import Path
from typing import Any

import aiofiles
import httpx
from fastapi import HTTPException
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from uwotm8 import convert_american_to_british_spelling

from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager
from transcribe_api.stt.utils import (
    cleanup_files,
    convert_input_dialogue_entries_to_dialogue_entries,
)
from transcribe_api.domain.models_dictation import DialogueEntry
from transcribe_api.runtime.logger import logger
from transcribe_api.runtime.settings_dictation import get_settings

TOO_MANY_REQUESTS = 429
# Azure batch transcription profanity options are: Masked, Removed, None.
# Use "None" to preserve profanity exactly as spoken (verbatim output).
VERBATIM_PROFANITY_FILTER_MODE = "None"


@retry(
    retry=retry_if_exception_type((Exception,)),  # Retry on any exception during download
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
)
async def download_audio_blob_with_retry(user_upload_blob_path: str) -> Path:
    """
    Download audio blob from Azure Storage with retry logic.

    Uses AsyncAzureBlobManager with DefaultAzureCredential (Managed Identity).
    Retries up to 3 times on network/transient errors.
    Returns the path to the downloaded temporary file.

    Parameters
    ----------
    user_upload_blob_path : str
        The path to the blob in Azure Storage.

    Returns
    -------
    Path
        Path to the temporary file containing the downloaded audio.

    Raises
    ------
    FileNotFoundError
        If the blob doesn't exist in storage.
    Exception
        If download fails after all retries.
    """
    file_extension = Path(user_upload_blob_path).suffix.lower()
    temp_file_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
            temp_file_path = Path(temp_file.name)

        logger.info(f"Downloading file from Azure Blob Storage: {user_upload_blob_path} to {temp_file_path}")

        # Use AsyncAzureBlobManager with Managed Identity (DefaultAzureCredential)
        async with AsyncAzureBlobManager() as blob_manager:
            success = await blob_manager.download_blob_to_file(
                blob_name=user_upload_blob_path,
                file_path=temp_file_path,
            )
            if not success:
                error_msg = f"Failed to download blob: {user_upload_blob_path}"
                raise RuntimeError(error_msg)

        logger.info(f"Successfully downloaded blob to {temp_file_path}")

    except Exception:
        # Clean up temp file on error
        if temp_file_path and temp_file_path.exists():
            await cleanup_files(temp_file_path)
        raise
    else:
        return temp_file_path


async def transcribe_audio(user_upload_s3_file_key: str) -> list[DialogueEntry]:
    result = await perform_transcription_steps_with_azure_and_aws(user_upload_s3_file_key)

    # Convert to British English spelling regardless of which service was used
    for entry in result:
        entry.text = convert_american_to_british_spelling(entry.text)

    return result


async def perform_transcription_steps_with_azure_and_aws(
    user_upload_blob_path: str,
) -> list[DialogueEntry]:
    """
    Download blob and transcribe audio.

    Blob download has retry logic (3 attempts).
    Transcription has limited retry logic (2 attempts max).
    """
    temp_file_path = None

    try:
        # Download with retry (3 attempts for network resilience)
        temp_file_path = await download_audio_blob_with_retry(user_upload_blob_path)

        # Transcribe (max 2 attempts - fast fail on API errors)
        result = await transcribe_audio_with_azure(temp_file_path)

    except Exception:
        await cleanup_files(temp_file_path)
        raise
    else:
        await cleanup_files(temp_file_path)
        return result


def convert_to_dialogue_entries(transcript_data: list[dict]) -> list[DialogueEntry]:
    """
    Convert transcript data into a list of DialogueEntry objects.

    Args:
        transcript_data: List of transcript segments with speaker, text, and timing info

    Returns:
        List of DialogueEntry objects
    """
    dialogue_entries = []

    for segment in transcript_data:
        entry = DialogueEntry(
            speaker=segment["speaker_label"],
            text=segment["transcript"],
            start_time=float(segment["start_time"]),
            end_time=float(segment["end_time"]),
        )
        dialogue_entries.append(entry)

    return dialogue_entries


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException)),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    stop=stop_after_attempt(2),  # Reduced from 5 to 2 (1 retry max) - fast fail on API errors
)
async def transcribe_audio_with_azure(audio_file_path: Path):
    """
    Async version of transcribe audio using Azure Speech-to-Text API.

    Retries once on HTTP errors or timeouts (max 2 attempts total).
    Transcription API errors are usually not transient, so we fail fast.
    """

    settings = get_settings()
    if not settings.AZURE_SPEECH_KEY:
        raise HTTPException(status_code=500, detail="AZURE_SPEECH_KEY not set")
    if not settings.AZURE_SPEECH_ENDPOINT:
        raise HTTPException(status_code=500, detail="AZURE_SPEECH_ENDPOINT not set")

    url = f"{settings.AZURE_SPEECH_ENDPOINT.rstrip('/')}/speechtotext/transcriptions:transcribe?api-version=2024-11-15"
    headers = {"Ocp-Apim-Subscription-Key": settings.AZURE_SPEECH_KEY}
    async with aiofiles.open(audio_file_path, "rb") as audio_file:
        audio_content = await audio_file.read()
        files: Any = {
            "audio": (audio_file_path.name, audio_content),
            "definition": (
                None,
                json.dumps(
                    {
                        "locales": ["en-GB"],
                        "diarization": {"enabled": True},
                        "profanityFilterMode": VERBATIM_PROFANITY_FILTER_MODE,
                    }
                ),
            ),
        }

        timeout_settings = httpx.Timeout(
            timeout=900.0,
            connect=900.0,
            read=900.0,
            write=900.0,
        )

        async with httpx.AsyncClient(timeout=timeout_settings) as client:
            response = await client.post(url, headers=headers, files=files)
            if response.status_code == TOO_MANY_REQUESTS:
                response.raise_for_status()

            full_response = response.json()

            # Check for nested error response shape: {"error": {"code": "...", "message": "..."}}
            if "error" in full_response and isinstance(full_response["error"], dict):
                error_message = full_response["error"].get("message", "Unknown error occurred")
                logger.error(f"Azure API error (nested): {error_message}")
                raise HTTPException(status_code=422, detail=error_message)

            # Check for top-level error response: {"code": "...", "message": "..."}
            if "code" in full_response:
                error_message = full_response.get("message", "Unknown error occurred")
                logger.error(f"Azure API error: {error_message}")
                raise HTTPException(status_code=422, detail=error_message)

            # If no error, proceed with phrases extraction
            phrases = full_response.get("phrases")
            if not phrases:
                raise HTTPException(
                    status_code=500,
                    detail="No transcription phrases found in response",
                )

            return convert_input_dialogue_entries_to_dialogue_entries(phrases)
