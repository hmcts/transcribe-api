import asyncio
import json
import tempfile
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytz
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import JSONResponse, StreamingResponse

from transcribe_api.api.document_content_models import DocumentContentResponse
from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager, _sanitize_for_log
from transcribe_api.stt.utils import (
    generate_blob_download_url,
    generate_blob_upload_url,
    get_file_blob_path,
)
from transcribe_api.domain.interface_dictation import (
    add_tag_to_transcription,
    delete_live_draft,
    delete_transcription_by_id,
    fetch_transcriptions_metadata,
    get_live_draft,
    get_minute_version_by_id,
    get_minute_versions,
    get_tags_for_transcription,
    get_transcription_by_id,
    get_transcription_jobs,
    get_user_by_id,
    mark_user_onboarding_complete,
    remove_tag_from_transcription,
    save_minute_version,
    save_transcription,
    save_transcription_job,
    update_user,
    upsert_live_draft,
)
from transcribe_api.domain.models_dictation import (
    DialogueEntry,
    MinuteVersion,
    Tag,
    Transcription,
    TranscriptionJob,
    User,
)
from transcribe_api.documents.content_loader import get_document_content
from transcribe_api.documents.models import DEFAULT_JURISDICTION
from transcribe_api.documents.template_renderer import render_hearing_document
from transcribe_api.documents.llm.llm_client import (
    langfuse_client,
)
from transcribe_api.runtime.logger import logger
from transcribe_api.documents.minutes.llm_calls import ai_edit_task, generate_llm_output_task, generate_realtime_summary
from transcribe_api.documents.minutes.templates.templates_metadata import (
    get_all_templates,
)
from transcribe_api.documents.minutes.types import (
    GenerateMinutesRequest,
    GenerateSummaryRequest,
    GenerateSummaryResponse,
    OnboardingStatusResponse,
    TemplateResponse,
    TranscriptionMetadata,
    UpdateUserRequest,
    UploadUrlRequest,
    UploadUrlResponse,
)
from transcribe_api.domain.auth.approles_dictation import get_role, has_any_role
from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user, get_current_user
from transcribe_api.documents.llm.langfuse_models import (
    LangfuseScoreRequest,
    LangfuseTraceRequest,
)
from transcribe_api.runtime.settings_dictation import get_settings
from transcribe_api.runtime.version import APP_VERSION

router = APIRouter()


def _validate_blob_ownership(blob_path: str, current_user: "User", request: Request) -> None:
    """Validate that a blob path's ownership segment belongs to the requesting user.

    Accepts both new OID-based paths (user-uploads/{azure_user_id}/...) and legacy
    email-based paths (user-uploads/{email}/...) during the Phase 1 transition window.

    Phase 1: New uploads use OID-based paths; legacy email paths remain accessible.
    Phase 2 (separate ticket, optional): Backfill script renames legacy blobs to OID paths
    and removes this dual-format tolerance.
    """
    if not blob_path.startswith("user-uploads/"):
        return

    parts = blob_path.split("/")
    if len(parts) < 2 or not parts[1]:  # noqa: SIM102
        raise HTTPException(status_code=400, detail="Invalid blob path")

    segment = parts[1]

    matches = (
        segment == str(current_user.azure_user_id)
        or segment.lower() == current_user.email.lower()
    )

    if not matches:
        logger.warning(
            "UNAUTHORISED_ACCESS_ATTEMPT user_id=%s user_email=%s "
            "reason=blob_ownership_mismatch attempted_resource=%s",
            _sanitize_for_log(current_user.id),
            _sanitize_for_log(current_user.email),
            _sanitize_for_log(f"{request.method} {request.url.path}"),
        )
        raise HTTPException(status_code=403, detail="Access denied")


# Azure Blob Storage configuration is handled through get_settings()


UK_TIMEZONE = pytz.timezone("Europe/London")

@router.get("/health")
async def health_check():
    return JSONResponse(status_code=200, content={"status": "ok", "version": APP_VERSION})


@router.get("/healthcheck")
async def health_check_legacy():
    """Legacy endpoint for backwards compatibility"""
    return JSONResponse(status_code=200, content={"status": "ok", "version": APP_VERSION})


@router.get("/document-content", response_model=DocumentContentResponse)
async def get_document_content_route(
    response: Response,
    current_user: User = Depends(get_allowlisted_user()),  # noqa: B008, ARG001
) -> DocumentContentResponse:
    """Return editable document content for allowlisted recorder users."""
    response.headers["Cache-Control"] = "no-store"
    data = get_document_content()
    return DocumentContentResponse(
        hearing_types=data.get("hearing_types", []),
        legal_frameworks=data.get("legal_frameworks", []),
        anonymity=data.get("anonymity", {}),
    )


@router.get("/user/onboarding-status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> OnboardingStatusResponse:
    """
    Get user's onboarding status and allowlist check.

    This endpoint provides comprehensive status information for the frontend
    to determine what UI to display to the user. It checks both onboarding
    completion status and allowlist membership.

    Parameters
    ----------
    current_user : User
        The authenticated user from the dependency injection.

    Returns
    -------
    OnboardingStatusResponse
        Complete status information including onboarding and allowlist status.

    Raises
    ------
    HTTPException
        If user authentication fails or allowlist check fails.
    """
    # Check if onboarding should be forced in development
    settings = get_settings()
    force_onboarding = settings.FORCE_ONBOARDING_DEV and settings.ENVIRONMENT in [
        "local",
        "dev",
    ]

    # Check whether the user holds any valid app role
    roles: list[str] = current_user.__dict__.get("app_roles", [])
    is_allowlisted = settings.ENVIRONMENT == "local" or has_any_role(roles)
    safe_roles = [_sanitize_for_log(r) for r in roles]
    logger.info(
        "App role check for user %s: roles=%s, is_allowlisted=%s",
        _sanitize_for_log(current_user.email),
        safe_roles,
        is_allowlisted,
    )

    return OnboardingStatusResponse(
        has_completed_onboarding=current_user.has_completed_onboarding,
        force_onboarding_override=force_onboarding,
        should_show_onboarding=(not current_user.has_completed_onboarding) or force_onboarding,
        user_id=current_user.id,
        environment=settings.ENVIRONMENT,
        is_allowlisted=is_allowlisted,
        should_show_coming_soon=not is_allowlisted,
    )


@router.post("/user/complete-onboarding")
async def complete_onboarding(
    current_user: User = Depends(get_allowlisted_user()),  # noqa: B008
):
    """Mark user's onboarding as complete"""

    # Don't update in dev override mode to preserve testing ability
    settings = get_settings()
    if not (settings.FORCE_ONBOARDING_DEV and settings.ENVIRONMENT in ["local", "dev"]):
        updated_user = mark_user_onboarding_complete(current_user.id)
        return {
            "success": True,
            "message": "Onboarding marked as complete",
            "has_completed_onboarding": updated_user.has_completed_onboarding,
        }
    else:
        return {
            "success": True,
            "message": "Onboarding completion skipped (dev override mode active)",
            "has_completed_onboarding": current_user.has_completed_onboarding,
        }


@router.post("/user/reset-onboarding")
async def reset_onboarding(
    current_user: User = Depends(get_allowlisted_user()),  # noqa: B008
):
    """Reset user's onboarding status (dev only)"""

    # Only allow in local/dev environments
    settings = get_settings()
    if settings.ENVIRONMENT not in ["local", "dev"]:
        raise HTTPException(
            status_code=403,
            detail="This endpoint is only available in local/dev environments",
        )

    # Reset onboarding status
    updated_user = update_user(current_user.id, has_completed_onboarding=False)

    return {
        "success": True,
        "message": "Onboarding status reset successfully",
        "has_completed_onboarding": updated_user.has_completed_onboarding,
        "user_id": str(updated_user.id),
        "email": updated_user.email,
    }


@router.get("/healthcheck/azure-storage")
async def azure_storage_health_check():
    """
    Health check endpoint to validate Azure Storage access using Managed Identity.
    This verifies that DefaultAzureCredential can authenticate and access the storage account.
    """
    try:
        # Try to access storage using Managed Identity
        async with AsyncAzureBlobManager() as blob_manager:
            # Make an actual API call to verify authentication and permissions
            # List blobs in the user-uploads prefix (lightweight operation)
            # This tests: authentication, container access, and permissions
            await blob_manager.list_blobs_in_prefix(
                prefix="user-uploads/",
                include_metadata=False,
            )
            container_name = blob_manager.container_name

        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "azure_storage": {
                    "valid": True,
                    "authentication": "managed_identity",
                    "container": container_name,
                },
            },
        )
    except Exception as e:
        return JSONResponse(
            status_code=503,  # Service Unavailable
            content={
                "status": "error",
                "azure_storage": {
                    "valid": False,
                    "error": f"Failed to access Azure Storage with Managed Identity: {e!s}",
                },
            },
        )


@router.post("/get-upload-url", response_model=UploadUrlResponse)
async def get_upload_url(
    request: UploadUrlRequest,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
) -> UploadUrlResponse:
    """Generate an upload URL for the client.

    When DIRECT_SDK_ACCESS is enabled, returns a short-lived Azure Blob SAS URL
    for direct browser-to-storage upload. Otherwise returns a backend proxy URL
    so the upload flows through the app domain.
    """
    settings = get_settings()
    file_name = f"{uuid.uuid4()}.{request.file_extension}"
    user_upload_blob_path = get_file_blob_path(str(current_user.azure_user_id), file_name)

    if settings.DIRECT_SDK_ACCESS:
        upload_url = await generate_blob_upload_url(
            container_name=settings.AZURE_STORAGE_CONTAINER_NAME,
            blob_name=user_upload_blob_path,
            expiry_hours=settings.UPLOAD_URL_TTL_MINUTES / 60,
        )
    else:
        encoded_key = urllib.parse.quote(user_upload_blob_path, safe="")
        upload_url = f"{settings.APP_URL.rstrip('/')}/api/upload-audio?key={encoded_key}"

    return UploadUrlResponse(
        upload_url=upload_url,
        user_upload_s3_file_key=user_upload_blob_path,
    )


@router.put("/upload-audio")
async def upload_audio_proxy(
    key: str,
    request: Request,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """Receive a raw audio upload and write it to blob storage.

    Used when DIRECT_SDK_ACCESS is disabled — the frontend PUTs the file here
    rather than directly to Azure Blob Storage.
    """
    if not key.startswith("user-uploads/"):
        raise HTTPException(status_code=400, detail="Invalid upload key")

    _validate_blob_ownership(key, current_user, request)

    body = await request.body()
    async with AsyncAzureBlobManager() as blob_manager:
        success = await blob_manager.create_blob_from_bytes(body, key)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to upload file to storage")

    return Response(status_code=200)


def _validate_transcription_for_minutes(transcription_id: UUID, user_id: UUID) -> list:
    """
    Validate that a transcription has dialogue entries for minute generation.

    Parameters
    ----------
    transcription_id : UUID
        ID of the transcription to validate
    user_id : UUID
        ID of the user requesting access

    Returns
    -------
    list
        List of dialogue entries from all transcription jobs

    Raises
    ------
    HTTPException
        If transcription not found, no jobs exist, or no dialogue entries available
    """
    # Verify user has access to this transcription
    get_transcription_by_id(
        transcription_id,
        user_id,
        tz=pytz.UTC,
    )

    # Fetch transcription jobs
    transcription_jobs = get_transcription_jobs(transcription_id)

    if not transcription_jobs:
        raise HTTPException(
            status_code=404,
            detail="No transcription jobs found. Cannot generate minutes.",
        )

    # Extract all dialogue entries
    dialogue_entries = []
    for job in transcription_jobs:
        dialogue_entries.extend(job.dialogue_entries)

    # Validate dialogue entries exist (catches "500: No transcription phrases" case)
    if not dialogue_entries:
        logger.warning(
            f"No dialogue entries found for transcription {transcription_id}. "
            f"Found {len(transcription_jobs)} job(s) but no dialogue entries."
        )
        raise HTTPException(
            status_code=424,
            detail="No dialogue entries found. Transcription may have failed or contained no speech.",
        )

    return dialogue_entries


@router.post("/generate-or-edit-minutes")
async def generate_or_edit_minutes(
    request: GenerateMinutesRequest,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """
    Initiate minute generation or editing as an async background task.

    Validates transcription has dialogue entries before starting LLM processing.
    Returns immediately with a minute_version_id that can be polled for completion.

    Parameters
    ----------
    request : GenerateMinutesRequest
        Request containing transcription_id, template, and action type
    current_user : User
        Authenticated user from dependency injection

    Returns
    -------
    JSONResponse
        Contains minute_version_id and status="initiated"

    Raises
    ------
    HTTPException
        404 if transcription or jobs not found
        424 if no dialogue entries available (transcription failed)
        400 if edit_instructions missing for edit action
    """
    user_id = current_user.id
    user_email = current_user.email

    # Validate and get dialogue entries (raises HTTPException if invalid)
    dialogue_entries = _validate_transcription_for_minutes(request.transcription_id, user_id)

    # Validate edit_instructions BEFORE creating minute_version_id
    if request.action_type == "edit" and not request.edit_instructions:
        raise HTTPException(
            status_code=400,
            detail="edit_instructions are required for edit action",
        )

    # Only create ID after ALL validation passes
    new_minute_version_id = str(uuid.uuid4())

    async def process_request():
        if request.action_type == "generate":
            await generate_llm_output_task(
                dialogue_entries,
                request.transcription_id,
                request.template,
                user_email,
                minute_version_id=new_minute_version_id,
            )
        elif request.action_type == "edit":
            await ai_edit_task(
                dialogue_entries,
                request.current_minute_version_id,
                new_minute_version_id,
                request.edit_instructions,
                request.transcription_id,
                user_email,
            )

    asyncio.create_task(process_request())  # noqa: RUF006 - fire-and-forget background task
    return JSONResponse(content={"minute_version_id": new_minute_version_id, "status": "initiated"})


@router.get("/templates", response_model=TemplateResponse)
async def get_templates(
    current_user: User = Depends(get_current_user),  # noqa: B008, ARG001
):
    """Get all template categories (auth only, used during onboarding)."""
    templates = get_all_templates()
    return TemplateResponse(templates=templates)


@router.get("/transcriptions-metadata", response_model=list[TranscriptionMetadata])
async def get_transcriptions_metadata(
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
    timezone: str = "Europe/London",  # Optional parameter with UK default
):
    """Get metadata for all transcriptions. Requires Judge, LegalTextManager, or SystemAdministrator role."""
    logger.info("getting transcription metadata for user %s", _sanitize_for_log(current_user.id))

    # Get timezone (fallback to UK if invalid)
    try:
        tz = pytz.timezone(timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = UK_TIMEZONE

    return fetch_transcriptions_metadata(current_user.id, tz)


@router.get("/transcriptions/{transcription_id}", response_model=Transcription)
async def get_transcription(
    transcription_id: UUID,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
    timezone: str = "Europe/London",  # Optional parameter with UK default
):
    """Get a specific transcription by ID."""
    try:
        tz = pytz.timezone(timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = UK_TIMEZONE

    transcription = get_transcription_by_id(transcription_id, current_user.id, tz)
    logger.info(
        "TRANSCRIPT_VIEWED transcription_id=%s user_id=%s user_email=%s",
        _sanitize_for_log(transcription_id),
        _sanitize_for_log(current_user.id),
        _sanitize_for_log(current_user.email),
    )
    return transcription


@router.post("/transcriptions", response_model=Transcription)
async def save_transcription_route(
    transcription_data: Transcription,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """Save or update a transcription."""
    logger.info("saving transcription for user %s", _sanitize_for_log(current_user.id))

    return save_transcription(transcription_data, current_user.id)


@router.get("/transcriptions/{transcription_id}/document-download")
async def get_document_download_url(
    transcription_id: UUID,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """Generate a fresh, time-limited download URL for a hearing document.

    The document blob path is stored at submission time.  This endpoint
    generates a new SAS token on each request so the URL never expires
    before the user can click it.
    """
    try:
        tz = UK_TIMEZONE
        transcription = get_transcription_by_id(transcription_id, current_user.id, tz)
    except Exception:
        raise HTTPException(status_code=404, detail="Transcription not found")  # noqa: B904

    # Get the blob path from the transcription job
    blob_path: str | None = None
    if transcription.transcription_jobs:
        blob_path = transcription.transcription_jobs[0].s3_audio_url

    if not blob_path:
        raise HTTPException(status_code=404, detail="No document found for this transcription")

    if blob_path.startswith("https://"):
        parsed = urllib.parse.urlparse(blob_path)
        path_parts = parsed.path.lstrip("/").split("/", 1)
        if len(path_parts) < 2:  # noqa: PLR2004
            raise HTTPException(status_code=500, detail="Stored document URL is malformed")
        blob_path = path_parts[1]

    # Generate a user-friendly download filename from the transcription title
    title = transcription.title or "hearing-document"
    hearing_date = transcription.created_datetime.strftime("%Y-%m-%d") if transcription.created_datetime else ""
    ext = Path(blob_path).suffix or ".docx"
    download_filename = f"[{hearing_date}] {title}{ext}" if hearing_date else f"{title}{ext}"

    settings = get_settings()
    if settings.DIRECT_SDK_ACCESS:
        download_url = await generate_blob_download_url(
            container_name=settings.AZURE_STORAGE_CONTAINER_NAME,
            blob_name=blob_path,
            expiry_hours=settings.DOWNLOAD_URL_TTL_MINUTES / 60,
            download_filename=download_filename,
        )
    else:
        download_url = f"{settings.APP_URL.rstrip('/')}/api/download?transcription_id={transcription_id}"

    logger.info(
        "Generated fresh download URL for transcription %s (document: %s)",
        str(transcription_id),
        _sanitize_for_log(blob_path),
    )

    return {"download_url": download_url}


@router.get("/download")
async def download_document(  # noqa: C901
    transcription_id: UUID,
    request: Request,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """Stream a transcript document from blob storage, verifying ownership before serving."""
    try:
        transcription = get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)
    except Exception:
        raise HTTPException(status_code=404, detail="Transcription not found")  # noqa: B904

    blob_path: str | None = None
    if transcription.transcription_jobs:
        blob_path = transcription.transcription_jobs[0].s3_audio_url

    if not blob_path:
        raise HTTPException(status_code=404, detail="No document found for this transcription")

    if blob_path.startswith("https://"):
        parsed = urllib.parse.urlparse(blob_path)
        path_parts = parsed.path.lstrip("/").split("/", 1)
        if len(path_parts) < 2:  # noqa: PLR2004
            raise HTTPException(status_code=500, detail="Stored document URL is malformed")
        blob_path = path_parts[1]

    _validate_blob_ownership(blob_path, current_user, request)
    logger.info(
        "TRANSCRIPT_DOWNLOADED transcription_id=%s user_id=%s user_email=%s type=document",
        _sanitize_for_log(transcription_id),
        _sanitize_for_log(current_user.id),
        _sanitize_for_log(current_user.email),
    )

    title = transcription.title or "hearing-document"
    hearing_date = transcription.created_datetime.strftime("%Y-%m-%d") if transcription.created_datetime else ""
    ext = Path(blob_path).suffix or ".docx"
    download_filename = f"[{hearing_date}] {title}{ext}" if hearing_date else f"{title}{ext}"

    _dl_settings = get_settings()
    download_url = await generate_blob_download_url(
        container_name=_dl_settings.AZURE_STORAGE_CONTAINER_NAME,
        blob_name=blob_path,
        expiry_hours=_dl_settings.DOWNLOAD_URL_TTL_MINUTES / 60,
        download_filename=download_filename,
    )

    fetch_headers = {}
    if range_header := request.headers.get("range"):
        fetch_headers["Range"] = range_header

    client = httpx.AsyncClient()
    blob_req = client.build_request("GET", download_url, headers=fetch_headers)
    blob_response = await client.send(blob_req, stream=True)

    response_headers = {}
    for h in ["Content-Type", "Content-Disposition", "Content-Length", "Content-Range"]:
        if val := blob_response.headers.get(h.lower()):
            response_headers[h] = val

    async def generate():
        try:
            async for chunk in blob_response.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await blob_response.aclose()
            await client.aclose()

    return StreamingResponse(generate(), status_code=blob_response.status_code, headers=response_headers)


@router.delete("/transcriptions/{transcription_id}", status_code=204)
async def delete_transcription(
    transcription_id: UUID,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """Delete a specific transcription by ID."""
    delete_transcription_by_id(transcription_id, current_user.id)
    logger.info(
        "TRANSCRIPT_DELETED transcription_id=%s user_id=%s user_email=%s",
        _sanitize_for_log(transcription_id),
        _sanitize_for_log(current_user.id),
        _sanitize_for_log(current_user.email),
    )


@router.get(
    "/transcriptions/{transcription_id}/minute-versions/{minute_version_id}",
    response_model=MinuteVersion,
)
async def get_minute_version_by_id_route(
    transcription_id: UUID,
    minute_version_id: UUID,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """Get a specific minute version by its ID for a given transcription."""
    # Verify user has access to the associated transcription
    transcription = get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)
    if not transcription:
        raise HTTPException(status_code=404, detail="Transcription not found")

    # Get the specific minute version, ensuring it belongs to the transcription
    minute_version = get_minute_version_by_id(minute_version_id, transcription_id)
    return minute_version


@router.get(
    "/transcriptions/{transcription_id}/minute-versions",
    response_model=list[MinuteVersion],
)
async def get_minute_versions_route(
    transcription_id: UUID,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """Get all minute versions for a specific transcription."""
    # Verify user has access to this transcription
    transcription = get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)
    if not transcription:
        raise HTTPException(status_code=404, detail="Transcription not found")

    return get_minute_versions(transcription_id)


@router.post("/transcriptions/{transcription_id}/minute-versions", response_model=MinuteVersion)
async def save_minute_version_route(
    transcription_id: UUID,
    minute_data: MinuteVersion,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """Create or update a minute version for a transcription."""
    transcription = get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)
    if not transcription:
        raise HTTPException(status_code=404, detail="Transcription not found")

    minute_data.transcription_id = transcription_id

    # Save the minute version first
    saved_minute = save_minute_version(minute_data)

    # Then handle the additional logging if needed
    old_html_content = get_minute_version_by_id(minute_data.id, transcription_id).html_content

    # Only log the event if content has changed
    if old_html_content != minute_data.html_content:
        langfuse_client.event(
            trace_id=minute_data.trace_id,
            name="user-edit",
            input=old_html_content,
            output=minute_data.html_content,
        )

    return saved_minute


@router.post("/transcriptions/{transcription_id}/jobs", response_model=TranscriptionJob)
async def save_transcription_job_route(
    transcription_id: UUID,
    job_data: TranscriptionJob,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
) -> TranscriptionJob:
    """Create a new transcription job for a transcription."""
    # Verify user has access to this transcription
    transcription = get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)
    if not transcription:
        raise HTTPException(status_code=404, detail="Transcription not found")

    job = save_transcription_job(job=job_data)
    logger.info(
        "TRANSCRIPT_JOB_SUBMITTED transcription_id=%s job_id=%s user_id=%s user_email=%s",
        _sanitize_for_log(transcription_id),
        _sanitize_for_log(job.id),
        _sanitize_for_log(current_user.id),
        _sanitize_for_log(current_user.email),
    )
    return job


@router.get("/transcriptions/{transcription_id}/jobs", response_model=list[TranscriptionJob])
async def get_transcription_jobs_route(
    transcription_id: UUID,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
) -> list[TranscriptionJob]:
    """Get all transcription jobs for a specific transcription."""
    # Verify user has access to this transcription
    transcription = get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)
    if not transcription:
        raise HTTPException(status_code=404, detail="Transcription not found")

    return get_transcription_jobs(transcription_id)


@router.get("/transcriptions/{transcription_id}/jobs/{job_id}/audio")
async def stream_audio(  # noqa: C901
    transcription_id: UUID,
    job_id: UUID,
    request: Request,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """Stream audio from blob storage with HTTP Range support for seekable playback."""
    get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)

    jobs = get_transcription_jobs(transcription_id)
    job = next((j for j in jobs if j.id == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Transcription job not found")
    if not job.s3_audio_url:
        raise HTTPException(status_code=404, detail="No audio available for this job")

    stored_url = job.s3_audio_url
    if stored_url.startswith("https://"):
        parsed = urllib.parse.urlparse(stored_url)
        path_parts = parsed.path.lstrip("/").split("/", 1)
        if len(path_parts) < 2:  # noqa: PLR2004
            raise HTTPException(status_code=500, detail="Stored audio URL is malformed")
        blob_path = path_parts[1]
    else:
        blob_path = stored_url

    _validate_blob_ownership(blob_path, current_user, request)
    logger.info(
        "TRANSCRIPT_DOWNLOADED transcription_id=%s job_id=%s user_id=%s user_email=%s type=audio",
        _sanitize_for_log(transcription_id),
        _sanitize_for_log(job_id),
        _sanitize_for_log(current_user.id),
        _sanitize_for_log(current_user.email),
    )

    _audio_settings = get_settings()
    playback_url = await generate_blob_download_url(
        container_name=_audio_settings.AZURE_STORAGE_CONTAINER_NAME,
        blob_name=blob_path,
        expiry_hours=_audio_settings.DOWNLOAD_URL_TTL_MINUTES / 60,
    )

    fetch_headers = {}
    if range_header := request.headers.get("range"):
        fetch_headers["Range"] = range_header

    client = httpx.AsyncClient()
    blob_req = client.build_request("GET", playback_url, headers=fetch_headers)
    blob_response = await client.send(blob_req, stream=True)

    response_headers = {"Accept-Ranges": "bytes"}
    for h in ["Content-Type", "Content-Length", "Content-Range"]:
        if val := blob_response.headers.get(h.lower()):
            response_headers[h] = val

    async def generate():
        try:
            async for chunk in blob_response.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await blob_response.aclose()
            await client.aclose()

    return StreamingResponse(generate(), status_code=blob_response.status_code, headers=response_headers)


class LiveTranscriptMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True, extra="forbid")

    speaker: str
    text: str
    timestamp: str
    timestamp_ms: int | None = Field(default=None, alias="timestampMs")


class SectionedMessages(BaseModel):
    """Sectioned transcript messages organized by document section."""

    model_config = ConfigDict(extra="ignore")

    background: list[LiveTranscriptMessage] = Field(default_factory=list)
    evidence: list[LiveTranscriptMessage] = Field(default_factory=list)
    facts: list[LiveTranscriptMessage] = Field(default_factory=list)

    def get_all_messages(self) -> list[LiveTranscriptMessage]:
        """Get all messages as a flat list for backwards compatibility."""
        return self.background + self.evidence + self.facts


class LiveTranscriptionFormRequest(BaseModel):
    """Form submission payload from the live transcription page."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    case_id: str | None = Field(default=None, alias="caseId")
    location: str
    location_other: str | None = Field(default=None, alias="locationOther")
    jurisdiction: str = Field(default=DEFAULT_JURISDICTION)
    hearing_date: str = Field(alias="hearingDate")
    judge_name: str = Field(alias="judgeName")
    anonymity_order: str | None = Field(default=None, alias="anonymityOrder")
    appellant_name: str = Field(alias="appellantName")
    respondent: str
    hearing_type: str = Field(alias="hearingType")
    # Appellant representation fields
    appellant_rep_type: str | None = Field(default=None, alias="appellantRepType")
    appellant_rep_details: str | None = Field(default=None, alias="appellantRepDetails")
    # Respondent representation fields
    respondent_rep_type: str | None = Field(default=None, alias="respondentRepType")
    respondent_rep_name: str | None = Field(default=None, alias="respondentRepName")
    appealable_decision_date: str = Field(alias="appealableDecisionDate")
    legal_issues: list[str] = Field(default_factory=list, alias="legalIssues")
    document_type: str = Field(alias="documentType")
    next_hearing_type: str | None = Field(default=None, alias="nextHearingType")
    next_hearing_adjudicator: str | None = Field(default=None, alias="nextHearingAdjudicator")


class LiveTranscriptionSubmissionRequest(BaseModel):
    """Request model that accepts either sectioned or flat message formats."""

    model_config = ConfigDict(extra="forbid")

    form_data: LiveTranscriptionFormRequest
    messages: SectionedMessages | list[LiveTranscriptMessage]
    notes: str | None = Field(default=None, max_length=200_000)

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, notes: str | None) -> str | None:
        if notes is None:
            return None
        normalized = notes.strip()
        return normalized or None

    def get_sectioned_messages(self) -> SectionedMessages:
        """Get messages in sectioned format, converting if necessary."""
        if isinstance(self.messages, SectionedMessages):
            return self.messages
        # Legacy format: put all messages in background section
        return SectionedMessages(background=self.messages)


class LiveTranscriptionSubmissionResponse(BaseModel):
    blob_path: str
    document_blob_path: str
    document_download_url: str
    transcription_id: str
    message: str


class TranscriptViewerResponse(BaseModel):
    """Transcript payload for the hearing viewer."""

    model_config = ConfigDict(populate_by_name=True)

    messages: SectionedMessages = Field(default_factory=SectionedMessages)
    appellant_name: str | None = Field(default=None, alias="appellantName")


class LiveDraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: dict
    form_data: dict


class LiveDraftResponse(BaseModel):
    transcript: dict
    form_data: dict
    saved_at: datetime


@router.get(
    "/transcriptions/{transcription_id}/sectioned-transcript",
    response_model=TranscriptViewerResponse,
)
async def get_sectioned_transcript_route(
    transcription_id: UUID,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
) -> TranscriptViewerResponse:
    """Get the recorder-style sectioned transcript for a hearing if available."""
    transcription = get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)
    if not transcription:
        raise HTTPException(status_code=404, detail="Transcription not found")

    oid_prefix = get_file_blob_path(str(current_user.azure_user_id), "live-transcription-submission-")
    email_prefix = get_file_blob_path(current_user.email, "live-transcription-submission-")

    async with AsyncAzureBlobManager() as blob_manager:
        blobs_oid = await blob_manager.list_blobs_in_prefix(prefix=oid_prefix, include_metadata=True)
        blobs_email = await blob_manager.list_blobs_in_prefix(prefix=email_prefix, include_metadata=True)
        blobs = blobs_oid + blobs_email

        matching_blobs = [
            blob for blob in blobs if blob.get("metadata", {}).get("transcription_id") == str(transcription_id)
        ]

        if not matching_blobs:
            raise HTTPException(status_code=404, detail="Sectioned transcript not found")

        latest_blob = sorted(
            matching_blobs,
            key=lambda blob: blob.get("last_modified") or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )[0]

        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tmp_file:
            temp_path = Path(tmp_file.name)

        try:
            await blob_manager.download_blob_to_file(latest_blob["name"], temp_path)
            payload = json.loads(temp_path.read_text(encoding="utf-8"))
        finally:
            temp_path.unlink(missing_ok=True)

    return TranscriptViewerResponse(
        messages=SectionedMessages.model_validate(payload.get("messages", {})),
        appellantName=payload.get("form_data", {}).get("appellantName"),
    )


@router.post("/live-transcription/submission", response_model=LiveTranscriptionSubmissionResponse)
async def save_live_transcription_submission(  # noqa: PLR0915
    request: LiveTranscriptionSubmissionRequest,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
) -> LiveTranscriptionSubmissionResponse:
    """Persist live transcription form and transcript data as JSON in the audio uploads bucket."""
    blob_filename = f"live-transcription-submission-{uuid.uuid4()}.json"
    blob_path = get_file_blob_path(str(current_user.azure_user_id), blob_filename)

    # Get sectioned messages (handles both sectioned and legacy flat formats)
    sectioned_messages = request.get_sectioned_messages()

    # Session data payload on "Submit hearing" button click
    # Store messages in sectioned format for document generation
    payload = {
        "form_data": request.form_data.model_dump(by_alias=True, exclude_none=True),
        "messages": {
            "background": [msg.model_dump(by_alias=True, exclude_none=True) for msg in sectioned_messages.background],
            "evidence": [msg.model_dump(by_alias=True, exclude_none=True) for msg in sectioned_messages.evidence],
            "facts": [msg.model_dump(by_alias=True, exclude_none=True) for msg in sectioned_messages.facts],
        },
        "notes": request.notes,
        "submitted_at": datetime.now(UTC).isoformat(),
        "user_email": current_user.email,
        "azure_user_id": str(current_user.azure_user_id),
    }

    # Log the JSON payload to the terminal
    logger.info("=== Live transcription submission received ===")
    logger.info("JSON payload:\n%s", json.dumps(payload, indent=2))

    temp_json_path: Path | None = None
    temp_doc_path: Path | None = None

    try:
        # Save JSON payload to blob storage
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as tmp_file:
            temp_path = Path(tmp_file.name)
            json.dump(payload, tmp_file)

        async with AsyncAzureBlobManager() as blob_manager:
            upload_success = await blob_manager.create_blob_from_file(temp_path, blob_path)

            if not upload_success:
                raise HTTPException(status_code=500, detail="Failed to save submission data")

            logger.info(
                "Live transcription submission saved to blob %s for user %s",
                _sanitize_for_log(blob_path),
                _sanitize_for_log(current_user.email),
            )

            # Generate Word document from template
            temp_output_dir = Path(tempfile.gettempdir()) / "hearing_documents"
            temp_doc_path = render_hearing_document(
                request=request,
                output_dir=temp_output_dir,
                user_email=current_user.email,
            )

            logger.info(
                "Rendered hearing document to %s for user %s",
                _sanitize_for_log(str(temp_doc_path)),
                _sanitize_for_log(current_user.email),
            )

            # Upload the Word document to blob storage
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            doc_blob_filename = f"hearing-document-{timestamp}.docx"
            doc_blob_path = get_file_blob_path(str(current_user.azure_user_id), doc_blob_filename)

            doc_upload_success = await blob_manager.create_blob_from_file(temp_doc_path, doc_blob_path)
            if not doc_upload_success:
                raise HTTPException(status_code=500, detail="Failed to upload document to storage")

            logger.info(
                "Uploaded hearing document to blob %s for user %s",
                _sanitize_for_log(doc_blob_path),
                _sanitize_for_log(current_user.email),
            )

        # Generate a user-friendly download filename
        # Format: "[{date}] {Appellant} v {Respondent}.docx"
        hearing_date = request.form_data.hearing_date  # YYYY-MM-DD format
        appellant_name = request.form_data.appellant_name
        respondent_name = request.form_data.respondent

        # Sanitize names for filename (remove characters not allowed in filenames)
        def sanitize_filename_part(name: str) -> str:
            # Remove or replace characters not safe for filenames
            return "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()

        safe_appellant = sanitize_filename_part(appellant_name)
        safe_respondent = sanitize_filename_part(respondent_name)
        download_filename = f"[{hearing_date}] {safe_appellant} v {safe_respondent}.docx"

        # Create transcription record for the hearing
        transcription_title = f"{request.form_data.appellant_name} v {request.form_data.respondent}"
        transcription = Transcription(
            user_id=current_user.id,
            title=transcription_title,
        )
        saved_transcription = save_transcription(transcription, current_user.id)

        # Generate download URL for the document
        _settings = get_settings()
        if _settings.DIRECT_SDK_ACCESS:
            doc_download_url = await generate_blob_download_url(
                container_name=_settings.AZURE_STORAGE_CONTAINER_NAME,
                blob_name=doc_blob_path,
                expiry_hours=_settings.DOWNLOAD_URL_TTL_MINUTES / 60,
                download_filename=download_filename,
            )
            logger.info(
                "Generated download URL for hearing document '%s' (expires in %d minutes)",
                _sanitize_for_log(download_filename),
                _settings.DOWNLOAD_URL_TTL_MINUTES,
            )
        else:
            doc_download_url = f"{_settings.APP_URL.rstrip('/')}/api/download?transcription_id={saved_transcription.id}"

        # Convert live transcript messages to DialogueEntry format (all sections combined)
        all_messages = sectioned_messages.get_all_messages()
        dialogue_entries = [
            DialogueEntry(
                speaker=msg.speaker,
                text=msg.text,
                start_time=float(msg.timestamp_ms or 0) / 1000.0,  # Convert ms to seconds
                end_time=float(msg.timestamp_ms or 0) / 1000.0,  # Same as start for live transcription
            )
            for msg in all_messages
        ]

        # Create transcription job with the dialogue entries and document blob path.
        # We store the *blob path* (not the full SAS URL) so that a fresh,
        # time-limited download URL can be generated on demand — avoiding the
        # "AuthenticationFailed / signed expiry" error when the original SAS
        # token expires after 24 hours.
        transcription_job = TranscriptionJob(
            transcription_id=saved_transcription.id,
            dialogue_entries=dialogue_entries,
            s3_audio_url=doc_blob_path,
        )
        save_transcription_job(transcription_job)

        async with AsyncAzureBlobManager() as blob_manager:
            metadata_success = await blob_manager.set_blob_metadata(
                blob_name=blob_path,
                metadata={"transcription_id": str(saved_transcription.id)},
            )

        if not metadata_success:
            logger.warning(
                "Failed to attach transcription metadata to live submission blob %s",
                _sanitize_for_log(blob_path),
            )

        logger.info(
            "Created transcription record %s for hearing submission",
            str(saved_transcription.id),
        )

        return LiveTranscriptionSubmissionResponse(
            blob_path=blob_path,
            document_blob_path=doc_blob_path,
            document_download_url=doc_download_url,
            transcription_id=str(saved_transcription.id),
            message="Submission data saved and document generated",
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Error saving live transcription submission for user %s",
            _sanitize_for_log(current_user.email),
        )
        raise HTTPException(status_code=500, detail="Failed to save submission data") from None
    finally:
        # Clean up temporary files
        if temp_json_path and temp_json_path.exists():
            temp_json_path.unlink(missing_ok=True)
        if temp_doc_path and temp_doc_path.exists():
            temp_doc_path.unlink(missing_ok=True)


class AddTagRequest(BaseModel):
    name: str


@router.get("/transcriptions/{transcription_id}/tags", response_model=list[Tag])
async def get_transcription_tags_route(
    transcription_id: UUID,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
) -> list[Tag]:
    """Get all tags for a specific transcription."""
    return get_tags_for_transcription(transcription_id, current_user.id)


@router.post("/transcriptions/{transcription_id}/tags", response_model=Tag)
async def add_tag_to_transcription_route(
    transcription_id: UUID,
    tag_request: AddTagRequest,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
) -> Tag:
    """Add a tag to a transcription."""
    return add_tag_to_transcription(transcription_id, tag_request.name, current_user.id)


@router.delete("/transcriptions/{transcription_id}/tags/{tag_name}", status_code=204)
async def remove_tag_from_transcription_route(
    transcription_id: UUID,
    tag_name: str,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """Remove a tag from a transcription."""
    remove_tag_from_transcription(transcription_id, tag_name, current_user.id)


@router.get("/user", response_model=User)
async def get_current_user_route(
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    """Get the current user's details (auth only, no allowlist check)."""
    return get_user_by_id(current_user.id)


# Add missing routes that frontend expects
@router.get("/users/me", response_model=User)
async def get_current_user_me_route(
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    """Get the current user's details (auth only, no allowlist check)."""
    return get_user_by_id(current_user.id)


@router.get("/user/profile", response_model=User)
async def get_user_profile_route(
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    """Get the current user's profile (auth only, no allowlist check)."""
    return get_user_by_id(current_user.id)


@router.post("/user", response_model=User)  # changed from .patch to .post
async def update_current_user_route(
    request: UpdateUserRequest,
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    """Update the current user's details (auth only, used during onboarding)."""
    update_fields = request.model_dump(exclude_unset=True)
    return update_user(current_user.id, **update_fields)


@router.post("/langfuse/trace")
async def submit_langfuse_trace(
    request: LangfuseTraceRequest,
    current_user: User = Depends(get_allowlisted_user()),  # noqa: B008
):
    """Submit a trace to Langfuse via backend proxy for security."""
    try:
        # Submit general trace/event
        langfuse_client.event(
            trace_id=request.trace_id,
            name=request.name,
            metadata=request.metadata or {},
            input=request.input_data,
            output=request.output_data,
            user_id=current_user.email,
        )

        logger.info(
            "Langfuse trace '%s' submitted for trace %s by user %s",
            _sanitize_for_log(request.name),
            _sanitize_for_log(request.trace_id),
            _sanitize_for_log(current_user.email),
        )

    except Exception as e:
        _e = f"Failed to submit Langfuse trace: {e!s}"
        logger.error(_e)
        raise HTTPException(status_code=500, detail=_e) from e
    else:
        return {"success": True, "message": "Trace submitted successfully"}


@router.post("/langfuse/score")
async def submit_langfuse_score(
    request: LangfuseScoreRequest,
    current_user: User = Depends(get_allowlisted_user()),  # noqa: B008
):
    """Submit a score to Langfuse via backend proxy for security."""
    try:
        # Submit score using the backend Langfuse client
        langfuse_client.score(
            trace_id=request.trace_id,
            name=request.name,
            value=request.value,
            comment=request.comment,
            user_id=current_user.email,
        )

        logger.info(
            "Langfuse score submitted for trace %s by user %s",
            _sanitize_for_log(request.trace_id),
            _sanitize_for_log(current_user.email),
        )

    except Exception as e:
        _e = f"Failed to submit Langfuse score: {e!s}"
        logger.error(_e)
        raise HTTPException(status_code=500, detail=_e) from e
    else:
        return {"success": True, "message": "Score submitted successfully"}


@router.get("/get-speech-token")
async def get_speech_token(
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
):
    """
    Get Azure Speech Services token for real-time transcription.

    This endpoint fetches a temporary token from Azure Speech Services
    that can be used by the frontend for real-time speech recognition.
    Tokens are valid for 10 minutes.
    """
    settings = get_settings()

    if not settings.AZURE_SPEECH_ENDPOINT:
        logger.error("Azure Speech credentials not configured")
        raise HTTPException(status_code=500, detail="Azure Speech Services not configured")

    try:
        if settings.AZURE_SPEECH_RESOURCE_ID:
            # Managed Identity path — required when public network access is disabled.
            # Returns an AAD token in the format the Speech SDK expects for Entra auth.
            from azure.identity.aio import DefaultAzureCredential
            async with DefaultAzureCredential() as credential:
                token_response = await credential.get_token("https://cognitiveservices.azure.com/.default")
            token = f"aad#{settings.AZURE_SPEECH_RESOURCE_ID}#{token_response.token}"
            logger.info("Speech token (AAD/Managed Identity) generated for user %s", _sanitize_for_log(current_user.email))
        elif settings.AZURE_SPEECH_KEY:
            # API key STS path — requires public network access to the Speech endpoint.
            fetch_token_url = f"{settings.AZURE_SPEECH_ENDPOINT.rstrip('/')}/sts/v1.0/issueToken"
            headers = {"Ocp-Apim-Subscription-Key": settings.AZURE_SPEECH_KEY}
            async with httpx.AsyncClient() as client:
                response = await client.post(fetch_token_url, headers=headers)
                response.raise_for_status()
            token = response.text
            logger.info("Speech token (API key/STS) generated for user %s", _sanitize_for_log(current_user.email))
        else:
            raise HTTPException(
                status_code=500,
                detail="Azure Speech Services not configured: set AZURE_SPEECH_KEY or AZURE_SPEECH_RESOURCE_ID",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to fetch speech token: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to fetch speech token: {e!s}") from e

    endpoint = settings.AZURE_SPEECH_ENDPOINT if settings.DIRECT_SDK_ACCESS else settings.APP_URL
    return {"token": token, "endpoint": endpoint}


@router.post("/generate-realtime-summary")
async def generate_realtime_summary_endpoint(
    request: GenerateSummaryRequest,
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")])),  # noqa: B008
) -> GenerateSummaryResponse:
    """
    Generate a real-time summary of conversation transcript entries.

    This endpoint takes transcript entries from real-time speech recognition
    and generates an AI summary. It can update an existing summary with new
    entries or create a fresh summary.
    """
    try:
        logger.info(
            "Generating realtime summary for user %s with %d entries",
            _sanitize_for_log(current_user.email),
            len(request.transcript_entries),
        )

        # Convert Pydantic models to dicts for the LLM function
        transcript_dicts = [entry.model_dump() for entry in request.transcript_entries]

        # Call the LLM to generate summary
        summary = await generate_realtime_summary(
            transcript_entries=transcript_dicts, existing_summary=request.existing_summary
        )

        logger.info("Realtime summary generated successfully for user %s", _sanitize_for_log(current_user.email))

        return GenerateSummaryResponse(summary=summary)

    except Exception as e:
        logger.error(f"Error generating realtime summary: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate summary: {e!s}") from e


# ---------------------------------------------------------------------------
# Live transcript draft — server-side autosave for in-progress recordings
# ---------------------------------------------------------------------------


@router.get("/live-draft", response_model=LiveDraftResponse)
async def get_live_draft_route(
    current_user: User = Depends(get_allowlisted_user()),  # noqa: B008
) -> LiveDraftResponse:
    """Return the current user's in-progress draft, or 404 if none or expired."""
    draft = get_live_draft(current_user.id)
    if draft is None:
        raise HTTPException(status_code=404, detail="No draft found")
    saved_at = draft.updated_datetime or draft.created_datetime
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=UTC)
    return LiveDraftResponse(
        transcript=draft.transcript,
        form_data=draft.form_data,
        saved_at=saved_at,
    )


@router.put("/live-draft", status_code=204)
async def upsert_live_draft_route(
    payload: LiveDraftPayload,
    current_user: User = Depends(get_allowlisted_user()),  # noqa: B008
) -> None:
    """Create or overwrite the current user's in-progress draft."""
    upsert_live_draft(current_user.id, payload.transcript, payload.form_data)


@router.delete("/live-draft", status_code=204)
async def delete_live_draft_route(
    current_user: User = Depends(get_allowlisted_user()),  # noqa: B008
) -> None:
    """Delete the current user's in-progress draft (on restore, discard, or submit)."""
    delete_live_draft(current_user.id)
