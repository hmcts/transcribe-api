import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import ffmpeg
import httpx
from azure.storage.blob import BlobSasPermissions, generate_blob_sas

from transcribe_api.domain.models_dictation import DialogueEntry
from transcribe_api.runtime.logger import logger
from transcribe_api.runtime.log_sanitization import sanitize_for_log
from transcribe_api.runtime.settings_dictation import get_settings


def is_rate_limit_error(exception):
    """Check if the exception is due to rate limiting (HTTP 429)"""
    return (
        isinstance(exception, httpx.HTTPStatusError) and exception.response.status_code == 429  # noqa: PLR2004
    )


def get_file_blob_path(azure_user_id: str, file_name: str) -> str:
    """
    Generate a consistent blob path for user uploads.

    Args:
        azure_user_id (str): The Azure AD Object ID (OID) of the user uploading the file
        file_name (str): The file name including extension

    Returns:
        str: The generated blob path in the format 'user-uploads/{azure_user_id}/{filename}'
    """
    return f"user-uploads/{azure_user_id}/{file_name}"


def extract_transcription_id_from_blob_path(blob_path: str, user_identifier: str) -> str | None:
    """
    Extract a valid transcription ID from the blob path filename.

    Attempts to use the filename (without extension) as the transcription ID if it's
    a valid UUID format. Falls back to None (auto-generation) for non-UUID filenames.

    Args:
        blob_path (str): Full path to the blob (e.g., 'user-uploads/{oid}/uuid.mp4')
        user_identifier (str): User identifier for logging purposes (OID or email)

    Returns:
        str | None: Valid UUID string to use as transcription_id, or None to trigger auto-generation
    """
    filename = Path(blob_path).stem  # Gets filename without extension

    try:
        UUID(filename)  # Validate UUID format
    except ValueError:
        # Filename is not a valid UUID, signal to auto-generate one
        logger.warning(
            "User %s: Filename '%s' is not a valid UUID, will auto-generate transcription_id for blob: %s",
            sanitize_for_log(user_identifier),
            sanitize_for_log(filename),
            sanitize_for_log(blob_path),
        )
        return None
    else:
        logger.info(
            "User %s: Using filename as transcription_id: %s",
            sanitize_for_log(user_identifier),
            sanitize_for_log(filename),
        )
        return filename


async def generate_blob_upload_url(container_name: str, blob_name: str, expiry_hours: int = 1) -> str:
    """
    Generate a time-limited SAS URL for uploading to Azure Blob Storage using Managed Identity.

    Uses User Delegation Key instead of account keys for enhanced security.
    Works in both local (via az login) and production (via managed identity) environments.
    The generated SAS token has write and create permissions only.

    Args:
        container_name (str): The name of the blob container
        blob_name (str): The name of the blob
        expiry_hours (int): How many hours the URL should be valid for (max 7 days)

    Returns:
        str: The presigned URL for uploading with SAS token

    Raises:
        Exception: If unable to authenticate or generate delegation key
    """
    from azure.identity.aio import DefaultAzureCredential
    from azure.storage.blob.aio import BlobServiceClient

    settings = get_settings()
    account_url = f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"

    # Authenticate using DefaultAzureCredential (no secrets needed)
    # - Local: Uses Azure CLI credentials (az login)
    # - Production: Uses App Service managed identity
    credential = DefaultAzureCredential()

    try:
        async with BlobServiceClient(account_url=account_url, credential=credential) as client:
            # Get user delegation key from Azure (proves our identity)
            start_time = datetime.now(UTC)
            expiry_time = start_time + timedelta(hours=expiry_hours)

            user_delegation_key = await client.get_user_delegation_key(
                key_start_time=start_time, key_expiry_time=expiry_time
            )

            # Generate SAS token using the delegation key
            sas_token = generate_blob_sas(
                account_name=settings.AZURE_STORAGE_ACCOUNT_NAME,
                container_name=container_name,
                blob_name=blob_name,
                user_delegation_key=user_delegation_key,
                permission=BlobSasPermissions(write=True, create=True),
                expiry=expiry_time,
            )

            return f"{account_url}/{container_name}/{blob_name}?{sas_token}"

    finally:
        await credential.close()


async def generate_blob_download_url(
    container_name: str,
    blob_name: str,
    expiry_hours: float,
    download_filename: str | None = None,
) -> str:
    """
    Generate a time-limited SAS URL for downloading from Azure Blob Storage using Managed Identity.

    Uses User Delegation Key instead of account keys for enhanced security.
    Works in both local (via az login) and production (via managed identity) environments.
    The generated SAS token has read permission only.

    Args:
        container_name: The name of the blob container.
        blob_name: The path of the blob to download.
        expiry_hours: How long the URL should remain valid (max 7 days).
        download_filename: Optional custom filename for the downloaded file.
            If provided, sets the Content-Disposition header to prompt download with this name.

    Returns:
        str: The presigned download URL with read permission and SAS token.

    Raises:
        Exception: If unable to authenticate or generate delegation key.
    """
    from azure.identity.aio import DefaultAzureCredential
    from azure.storage.blob.aio import BlobServiceClient

    settings = get_settings()
    account_url = f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"

    # Authenticate using DefaultAzureCredential (no secrets needed)
    # - Local: Uses Azure CLI credentials (az login)
    # - Production: Uses App Service managed identity
    credential = DefaultAzureCredential()

    try:
        async with BlobServiceClient(account_url=account_url, credential=credential) as client:
            # Get user delegation key from Azure (proves our identity)
            start_time = datetime.now(UTC)
            expiry_time = start_time + timedelta(hours=expiry_hours)

            user_delegation_key = await client.get_user_delegation_key(
                key_start_time=start_time, key_expiry_time=expiry_time
            )

            # Build SAS token parameters
            sas_params = {
                "account_name": settings.AZURE_STORAGE_ACCOUNT_NAME,
                "container_name": container_name,
                "blob_name": blob_name,
                "user_delegation_key": user_delegation_key,
                "permission": BlobSasPermissions(read=True),
                "expiry": expiry_time,
            }

            # Add content disposition for custom download filename
            if download_filename:
                sas_params["content_disposition"] = f'attachment; filename="{download_filename}"'

            # Generate SAS token using the delegation key
            sas_token = generate_blob_sas(**sas_params)

            return f"{account_url}/{container_name}/{blob_name}?{sas_token}"

    finally:
        await credential.close()


def convert_to_mp3(  # noqa: C901, PLR0912
    input_file_path: Path, output_file=None, bitrate="192k", vbr=None
) -> Path:
    """
    Convert any audio or video file to MP3 format using FFmpeg.

    Args:
    input_file (str): Path to the input audio or video file.
    output_file (str, optional): Path to the output MP3 file. If not provided,
                                 it will be generated based on the input file name.
    bitrate (str, optional): The bitrate for the output MP3 file. Default is '192k'.
    vbr (int, optional): VBR quality setting (0-9). If provided, overrides bitrate.
                         0 is highest quality, 9 is lowest. None means CBR is used.

    Returns:
    str: Path to the output MP3 file.

    Raises:
    FileNotFoundError: If the input file doesn't exist.
    RuntimeError: If FFmpeg encounters an error during conversion.
    ValueError: If invalid bitrate or VBR quality is provided.
    """

    temp_output = None

    if not Path(input_file_path).is_file():
        msg = f"Input file not found: {input_file_path}"
        # logger.error(msg)
        raise FileNotFoundError(msg)

    # Always generate an output filename if not provided
    if output_file is None:
        # input_path = Path(input_file_path)
        output_file = str(input_file_path.with_name(f"{input_file_path.stem}_converted.mp3"))

    # Validate bitrate format
    if not bitrate.endswith(("k", "K")) or not bitrate[:-1].isdigit():
        msg = f"Invalid bitrate format: {bitrate}. Use format like '192k'."
        # logger.error(msg)
        raise ValueError(msg)

    # Validate VBR quality
    if vbr is not None and not (0 <= vbr <= 9):  # noqa: PLR2004
        msg = f"Invalid VBR quality: {vbr}. Must be between 0 and 9."
        # logger.error(msg)
        raise ValueError(msg)

    try:
        # logger.info("Probing input file for audio streams")
        probe = ffmpeg.probe(input_file_path)
        audio_streams = [stream for stream in probe["streams"] if stream["codec_type"] == "audio"]

        if not audio_streams:
            msg = f"No audio stream found in the input file: {input_file_path}"
            # logger.error(msg)
            raise RuntimeError(msg)

        # Create a temporary file if the input is an MP3
        if str(input_file_path).lower().endswith(".mp3"):
            temp_output = tempfile.NamedTemporaryFile(  # noqa: SIM115
                suffix=".mp3", delete=False
            ).name
            final_output = output_file
        else:
            temp_output = output_file
            final_output = output_file
            # logger.info("Input is not MP3. No temporary file needed.")

        # Open the input file
        input_stream = ffmpeg.input(input_file_path)

        # Set up the output stream with the desired parameters
        output_args = {
            "acodec": "libmp3lame",  # Use LAME MP3 encoder
            "loglevel": "warning",  # Show warnings and errors
        }

        if vbr is not None:
            output_args["qscale:a"] = vbr  # Use VBR encoding
        else:
            output_args["audio_bitrate"] = bitrate  # Use CBR encoding

        output_stream = ffmpeg.output(input_stream, temp_output, **output_args)

        # Run the FFmpeg command
        ffmpeg.run(output_stream, overwrite_output=True)
        # logger.info("FFmpeg command completed successfully")

        # If we used a temporary file, replace the original
        if temp_output and temp_output != final_output:
            shutil.move(temp_output, final_output)

    except Exception:
        # logger.exception("Unexpected error occurred")
        # log error message:
        # logger.exception(e)

        # Clean up the temporary file if it was created
        if temp_output and Path(temp_output).exists():
            # logger.info("Removing temporary file: %s", temp_output)
            Path(temp_output).unlink()
        raise
    else:
        return output_file


def convert_input_dialogue_entries_to_dialogue_entries(
    entries: list,
) -> list[DialogueEntry]:
    return [
        DialogueEntry(
            speaker=str(entry["speaker"]),
            text=entry["text"],
            start_time=float(entry["offsetMilliseconds"]) / 1000,
            end_time=(float(entry["offsetMilliseconds"]) + float(entry["durationMilliseconds"])) / 1000,
        )
        for entry in entries
    ]


def get_audio_duration(file_path: Path) -> float:
    """
    Get the duration of an audio file in seconds using ffprobe.

    Args:
        file_path: Path to the audio file

    Returns:
        Duration in seconds

    Raises:
        ValueError: If the file cannot be processed or is invalid
    """
    try:
        logger.info("Getting audio duration using ffprobe")
        result = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            logger.error(f"ffprobe command failed with return code {result.returncode}")
            logger.error(f"ffprobe stderr: {result.stderr}")
            raise ValueError(  # noqa: TRY003
                "Failed to get duration using ffprobe"  # noqa: EM101
            )

        duration = float(result.stdout)
        logger.info(f"Successfully got duration using ffprobe: {duration} seconds")
        return duration  # noqa: TRY300

    except Exception as e:
        logger.error(f"Failed to get audio duration: {e!s}", exc_info=True)
        raise


async def cleanup_files(temp_path: Path | None) -> None:
    """Helper function to clean up temporary files and S3 objects."""
    try:
        # Clean up local files
        if temp_path and temp_path.exists():
            temp_path.unlink()
    except Exception as e:
        logger.error(f"Error cleaning up files: {e!s}", exc_info=True)


def get_url_for_transcription(transcription_id: UUID) -> str:
    # https://justice-transcribe.ai.cabinetoffice.gov.uk/?id=027fecb0-6d4f-4ecb-b742-161adb5bad22
    settings = get_settings()

    # Use hardcoded URL for production environment
    if settings.ENVIRONMENT == "prod":
        app_url = "https://transcription.service.justice.gov.uk"
    else:
        app_url = settings.APP_URL
        if not app_url.startswith("https://"):
            app_url = f"https://{app_url.removeprefix('http://')}"

    return f"{app_url}/?id={transcription_id}"
