import asyncio
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

import aiofiles
import httpx
from fastapi import HTTPException
from hmcts_otel import Outcome, record_speech_usage
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from uwotm8 import convert_american_to_british_spelling

from transcribe_api.domain.models_dictation import DialogueEntry
from transcribe_api.domain.provenance_dictation import SpeechConfiguration, endpoint_reference
from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager, _redact_blob_path
from transcribe_api.runtime.logger import logger
from transcribe_api.runtime.settings_dictation import get_settings
from transcribe_api.stt.speech_auth import SpeechAuthError, speech_auth_headers
from transcribe_api.stt.utils import (
    cleanup_files,
    convert_input_dialogue_entries_to_dialogue_entries,
    get_audio_duration,
)

TOO_MANY_REQUESTS = 429
# OBS-5: the AI usage feature of uploaded-audio transcription (hmcts.speech.audio.duration).
UPLOAD_TRANSCRIPTION_FEATURE = "upload_transcription"
# Azure statuses where the service, not the audio, failed: authentication,
# permissions/quota, throttling, a wrong endpoint or API version (404), a timeout (408)
# and a conflict (409). These are retried; only the rest of 4xx (the request or the
# audio rejected) mark the file as failed for good.
_SERVICE_SIDE_CLIENT_ERRORS = frozenset({401, 403, 404, 408, 409, TOO_MANY_REQUESTS})


# Azure error codes are identifiers ("InvalidRequest", "401"); anything else is
# not logged. Messages are never logged: they can carry request content.
_AZURE_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _loggable_azure_code(code: object) -> str:
    return code if isinstance(code, str) and _AZURE_ERROR_CODE.fullmatch(code) else "unrecognised"


NO_SPEECH_DETECTED = "speech.no_speech_detected"


class NoSpeechDetectedError(HTTPException):
    """Azure transcribed the audio but found no speech in it (silent or speech-free
    audio): the user's recording, not a service error, so a 422 (a handled failure).
    ``error_code`` names it for telemetry and the user-facing message."""

    error_code = NO_SPEECH_DETECTED

    def __init__(self) -> None:
        super().__init__(status_code=422, detail="No speech was detected in the recording")


def _azure_error_status(response_status: int) -> int:
    """422 (the audio or request was rejected) only for Azure's own client errors;
    502 (a dependency failure, worth retrying) for auth, quota, throttling, outages
    and anything else. The user-facing message depends on this (user_error_message)."""
    if 400 <= response_status < 500 and response_status not in _SERVICE_SIDE_CLIENT_ERRORS:
        return 422
    return 502
# Azure batch transcription profanity options are: Masked, Removed, None.
# Use "None" to preserve profanity exactly as spoken (verbatim output).
VERBATIM_PROFANITY_FILTER_MODE = "None"
# Fast transcription request settings: sent to Azure (transcribe_audio_with_azure)
# and recorded as the transcript's provenance (DIAAT-286) from this one definition.
FAST_TRANSCRIPTION_API_VERSION = "2024-11-15"
FAST_TRANSCRIPTION_LOCALES = ("en-GB",)
FAST_TRANSCRIPTION_DIARIZATION = True


def speech_configuration() -> SpeechConfiguration:
    """The fast transcription configuration in use (sent to Azure, and its provenance)."""
    host, region = endpoint_reference(get_settings().AZURE_SPEECH_ENDPOINT)
    return SpeechConfiguration(
        endpoint_host=host,
        region=region,
        api_version=FAST_TRANSCRIPTION_API_VERSION,
        locales=list(FAST_TRANSCRIPTION_LOCALES),
        diarization=FAST_TRANSCRIPTION_DIARIZATION,
        profanity_filter_mode=VERBATIM_PROFANITY_FILTER_MODE,
    )


def _transcription_definition(config: SpeechConfiguration) -> dict[str, Any]:
    return {
        "locales": config.locales,
        "diarization": {"enabled": config.diarization},
        "profanityFilterMode": config.profanity_filter_mode,
    }


# The detail of an HTTPException raised for an Azure error: never Azure's own message,
# which can quote the request, and which travels with the exception (exception
# events). The status and Azure's code are logged; user_error_message builds what the
# user sees from the status.
_AZURE_REJECTED = "Azure Speech returned an error"


def _reported_audio_seconds(response: dict[str, Any]) -> float | None:
    """The audio duration Azure reports for the request (``durationMilliseconds``), in
    seconds: the quantity fast transcription is billed on. None if absent or invalid."""
    value = response.get("durationMilliseconds")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value / 1000 if math.isfinite(value) and value >= 0 else None


async def _record_fast_transcription_usage(
    status_code: int, response: dict[str, Any], audio_file_path: Path, outcome: Outcome = Outcome.SUCCESS
) -> None:
    """Record the audio seconds of a request Azure processed (OBS-5). Usage only.

    Only a response Azure transcribed (HTTP 2xx, no error body) is recorded: that is
    what it bills. Requests it rejected or failed are not billed and not recorded.
    The duration Azure reports is preferred; otherwise the file is measured (ffprobe).
    If neither is available nothing is recorded (and a warning is logged): a guess
    would skew the usage share that cost is allocated by.
    """
    if not 200 <= status_code < 300:  # noqa: PLR2004 - not transcribed, so not billed
        return
    seconds = _reported_audio_seconds(response)
    if seconds is None:
        try:
            seconds = await asyncio.to_thread(get_audio_duration, audio_file_path)
        except Exception:
            logger.warning("Fast transcription audio duration unknown: usage not recorded")
            return
    record_speech_usage(seconds=seconds, mode="fast", feature=UPLOAD_TRANSCRIPTION_FEATURE, outcome=outcome)


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

        logger.info(f"Downloading file from Azure Blob Storage: {_redact_blob_path(user_upload_blob_path)} to {temp_file_path}")

        # Use AsyncAzureBlobManager with Managed Identity (DefaultAzureCredential)
        async with AsyncAzureBlobManager() as blob_manager:
            success = await blob_manager.download_blob_to_file(
                blob_name=user_upload_blob_path,
                file_path=temp_file_path,
            )
            if not success:
                error_msg = f"Failed to download blob: {_redact_blob_path(user_upload_blob_path)}"
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
    if not settings.AZURE_SPEECH_ENDPOINT:
        raise HTTPException(status_code=500, detail="AZURE_SPEECH_ENDPOINT not set")

    config = speech_configuration()
    url = (
        f"{settings.AZURE_SPEECH_ENDPOINT.rstrip('/')}/speechtotext/transcriptions:transcribe"
        f"?api-version={config.api_version}"
    )
    # Workload identity bearer token; the key header only for local dev.
    try:
        headers = await speech_auth_headers(settings.AZURE_SPEECH_KEY)
    except SpeechAuthError as exc:
        logger.error("Could not authenticate to Azure Speech (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not authenticate to Azure Speech") from exc
    async with aiofiles.open(audio_file_path, "rb") as audio_file:
        audio_content = await audio_file.read()
        files: Any = {
            "audio": (audio_file_path.name, audio_content),
            "definition": (None, json.dumps(_transcription_definition(config))),
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
                logger.error(
                    "Azure API error (nested) status=%s code=%s",
                    response.status_code,
                    _loggable_azure_code(full_response["error"].get("code")),
                )
                raise HTTPException(status_code=_azure_error_status(response.status_code), detail=_AZURE_REJECTED)

            # Check for top-level error response: {"code": "...", "message": "..."}
            if "code" in full_response:
                logger.error(
                    "Azure API error status=%s code=%s",
                    response.status_code,
                    _loggable_azure_code(full_response.get("code")),
                )
                raise HTTPException(status_code=_azure_error_status(response.status_code), detail=_AZURE_REJECTED)

            # If no error, proceed with phrases extraction
            phrases = full_response.get("phrases")
            if isinstance(phrases, list) and not phrases:
                # Transcribed (and billed) but no speech found: the user's recording, a
                # failure outcome (not an error: it is not the service's fault).
                await _record_fast_transcription_usage(
                    response.status_code, full_response, audio_file_path, Outcome.FAILURE
                )
                raise NoSpeechDetectedError
            if not phrases:
                # A response without its phrases: the service's, an error outcome.
                await _record_fast_transcription_usage(
                    response.status_code, full_response, audio_file_path, Outcome.ERROR
                )
                raise HTTPException(
                    status_code=500,
                    detail="No transcription phrases found in response",
                )

            try:
                entries = convert_input_dialogue_entries_to_dialogue_entries(phrases)
            except Exception:
                # Transcribed (and billed) but unusable: an error outcome, as above.
                await _record_fast_transcription_usage(
                    response.status_code, full_response, audio_file_path, Outcome.ERROR
                )
                raise
            await _record_fast_transcription_usage(response.status_code, full_response, audio_file_path)
            return entries
