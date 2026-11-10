import asyncio
import json
import os
import re
import tempfile
import threading
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytz
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from hmcts_otel import StageHandle, background_span, journey, record_event, stage
from hmcts_otel import conventions as c
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlmodel import Session
from starlette.responses import JSONResponse, StreamingResponse

from transcribe_api import jurisdiction_templates
from transcribe_api.api.document_content_models import DocumentContentResponse
from transcribe_api.documents.content_loader import get_document_content, has_legal_wording
from transcribe_api.documents.llm.langfuse_models import (
    LangfuseScoreRequest,
    LangfuseTraceRequest,
)
from transcribe_api.documents.llm.llm_client import (
    langfuse_client,
    langfuse_trace_id,
)
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
from transcribe_api.documents.models import DEFAULT_JURISDICTION
from transcribe_api.documents.template_renderer import render_hearing_document
from transcribe_api.documents.uploaded_templates import (
    FillContext,
    box_text,
    check_values,
    dictated_text,
    fill_template,
)
from transcribe_api.documents.versioning import DECISION_TEMPLATE, track_render
from transcribe_api.domain.audit import (
    SECTIONED_TRANSCRIPT,
    TRANSCRIPT,
    TRANSCRIPT_DELETED,
    TRANSCRIPT_EXPORTED,
    TRANSCRIPT_VIEWED,
    AuditedAction,
    audited,
    deferred_audit,
)
from transcribe_api.domain.auth.approles_dictation import get_role, has_any_role
from transcribe_api.domain.auth.dependencies_dictation import (
    get_active_jurisdiction,
    get_allowlisted_user,
    get_current_user,
)
from transcribe_api.domain.blob_ownership import (
    USER_UPLOADS_PREFIX,
    blob_owner_segment,
    segment_belongs_to_user,
)
from transcribe_api.domain.exceptions import JurisdictionConflictError, OwnershipConflictError
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
    get_transcription_owner,
    get_user_by_id,
    mark_user_onboarding_complete,
    remove_tag_from_transcription,
    save_minute_version,
    save_transcription,
    save_transcription_job,
    update_user,
    upsert_live_draft,
)
from transcribe_api.domain.jurisdictions import (
    TEMPLATE_IAC_DECISION,
    ActiveJurisdiction,
    owned_transcription_jurisdiction,
    record_upload_jurisdiction,
)
from transcribe_api.domain.models_dictation import (
    DialogueEntry,
    MinuteVersion,
    Tag,
    Transcription,
    TranscriptionJob,
    User,
)
from transcribe_api.domain.provenance_dictation import decision_document_provenance
from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager, _redact_blob_path, _sanitize_for_log
from transcribe_api.runtime.browser_telemetry import telemetry_relay_router
from transcribe_api.runtime.db_connection import get_engine
from transcribe_api.runtime.dependency_health import get_dependency_checker, health_response
from transcribe_api.runtime.logger import logger
from transcribe_api.runtime.security_events import (
    AUDIO,
    DOCUMENT,
    MINUTE_VERSION,
    READ,
    TEST,
    TRANSCRIPTION,
    TRANSCRIPTION_JOB,
    WRITE,
    action_for_method,
    owner_matches,
    record_jurisdiction_blocked,
    record_ownership_violation,
)
from transcribe_api.runtime.settings_dictation import get_settings
from transcribe_api.runtime.settings_recording import get_settings as get_recording_settings
from transcribe_api.runtime.telemetry import JOURNEY_SUBMISSION, transcription_id_from_key, transcription_journey
from transcribe_api.runtime.version import APP_VERSION
from transcribe_api.stt.utils import (
    generate_blob_download_url,
    generate_blob_upload_url,
    get_file_blob_path,
)

# The app roles nearly every route requires. One dependency, so FastAPI resolves it
# once a request, and the jurisdiction check (DIAAT-334), which depends on it, runs
# after it: a caller without a role is refused as before, not recorded.
_ANY_APP_ROLE = get_allowlisted_user(
    required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")]
)


router = APIRouter()

# OBS-4: browser telemetry relay, POST /api/telemetry (same roles as dictation). A
# router of its own: every page sends to it, so it has no hmcts.area (shared).
telemetry_router = telemetry_relay_router(
    get_allowlisted_user(
        required_roles_any=[
            get_role("Normal"),
            get_role("Judge"),
            get_role("LegalTextManager"),
            get_role("SystemAdministrator"),
        ]
    )
)


def _validate_blob_ownership(
    blob_path: str,
    current_user: "User",
    request: Request,
    *,
    resource_type: str = DOCUMENT,
    action: str = READ,
    object_id: UUID | str | None = None,
) -> None:
    """Validate that a blob path's ownership segment belongs to the requesting user.

    Accepts both new OID-based paths (user-uploads/{azure_user_id}/...) and legacy
    email-based paths (user-uploads/{email}/...) during the Phase 1 transition window.
    The segment rules are shared with the background poller (domain.blob_ownership).

    Phase 1: New uploads use OID-based paths; legacy email paths remain accessible.
    Phase 2 (separate ticket, optional): Backfill script renames legacy blobs to OID paths
    and removes this dual-format tolerance.
    """
    if not blob_path.startswith(USER_UPLOADS_PREFIX):
        return

    segment = blob_owner_segment(blob_path)
    if segment is None:
        raise HTTPException(status_code=400, detail="Invalid blob path")

    if not segment_belongs_to_user(segment, current_user):
        logger.warning(
            "UNAUTHORISED_ACCESS_ATTEMPT user_id=%s "
            "reason=blob_ownership_mismatch attempted_resource=%s",
            _sanitize_for_log(current_user.id),
            _sanitize_for_log(f"{request.method} {request.url.path}"),
        )
        # object_id: the transcription the request named (DIAAT-286 audit record only).
        record_ownership_violation(resource_type, action, actor_id=current_user.id, object_id=object_id)
        raise HTTPException(status_code=403, detail="Access denied")


def _refuse_other_jurisdiction(
    transcription_id: UUID | str,
    current_user: User,
    jurisdiction: ActiveJurisdiction,
    resource_type: str = TRANSCRIPTION,
    action: str = READ,
) -> None:
    """DIAAT-334: the caller's record, but in a jurisdiction this request is not working
    in, is refused with the 404 of an unknown record, and recorded. Someone else's
    record, or none, is left to the ownership checks."""
    owned, record_jurisdiction = owned_transcription_jurisdiction(UUID(str(transcription_id)), current_user.id)
    if owned and record_jurisdiction != jurisdiction.id:
        record_jurisdiction_blocked(resource_type, action, actor_id=current_user.id, object_id=transcription_id)
        raise HTTPException(status_code=404, detail="Transcription not found")


def _in_jurisdiction(resource_type: str):
    """A route dependency for a route naming a transcription (path or query): its
    material is reachable only in its own jurisdiction (DIAAT-334). ``resource_type``
    is the route's, stated, not read from the URL. A plain def: the lookup is
    synchronous, so FastAPI runs it in the threadpool."""

    def check(
        request: Request,
        transcription_id: UUID,
        current_user: User = Depends(_ANY_APP_ROLE),
        jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
    ) -> None:
        _refuse_other_jurisdiction(
            transcription_id, current_user, jurisdiction, resource_type, action_for_method(request.method)
        )

    return Depends(check)


def _owned_transcription(transcription_id: UUID, current_user: User, resource_type: str, tz=None) -> Transcription:
    """The caller's transcription, with the post-authorisation assertion (DIAAT-285)
    in the one lookup the reading routes share, so none can miss it. 404 for an
    unknown ID or another user's."""
    transcription = get_transcription_by_id(transcription_id, current_user.id, tz or UK_TIMEZONE)
    if not transcription:
        raise HTTPException(status_code=404, detail="Transcription not found")
    _assert_owned(transcription, current_user, resource_type)
    return transcription


def _assert_owned(transcription: Transcription, current_user: User, resource_type: str) -> None:
    """Post-authorisation assertion (DIAAT-285): the record about to be returned is the
    caller's. The lookup already filters by owner, so this never fires with correct
    code; if it does, the request gets the same 404 as an unknown ID and Security is
    alerted (security.ownership_violation, outcome "detected")."""
    if not owner_matches(transcription.user_id, current_user.id, resource_type=resource_type, object_id=transcription.id):
        raise HTTPException(status_code=404, detail="Transcription not found")


# Azure Blob Storage configuration is handled through get_settings()


UK_TIMEZONE = pytz.timezone("Europe/London")

@router.get("/health")
async def health_check():
    return JSONResponse(status_code=200, content={"status": "ok", "version": APP_VERSION})


@router.get("/health/dependencies")
async def dependency_health_check():
    """Last known health of each dependency (DIAAT-277), from the background checker.

    Never runs a check: it reads the cache, so it is cheap and cannot hang. 200 when
    no dependency is down, 503 when any is ("unknown" until the first check). Not the
    liveness or readiness probe: those stay /health, so a dependency outage does not
    restart every pod. Unauthenticated like /health: names and states only. Not
    traced (hmcts-otel excludes /health paths).
    """
    status_code, body = health_response(get_dependency_checker().statuses())
    return JSONResponse(status_code=status_code, content=body, headers={"Cache-Control": "no-store"})


# The controlled alert test may run only here, and only with
# SECURITY_ALERT_TEST_TRIGGER_ENABLED: never in a deployed CNP environment (preview and
# aat, perftest, ithc, demo, prod) unless this list is changed on purpose.
SECURITY_ALERT_TEST_ENVIRONMENTS = frozenset({"local", "test"})


def security_alert_test_allowed() -> bool:
    """Fail closed: ENVIRONMENT must be set explicitly to an allowed value. The
    settings default ("local") does not count, so a pod whose ENVIRONMENT is missing
    or blank cannot run the test whatever the flag says."""
    environment = os.environ.get("ENVIRONMENT", "").strip().lower()
    return get_recording_settings().SECURITY_ALERT_TEST_TRIGGER_ENABLED and environment in SECURITY_ALERT_TEST_ENVIRONMENTS


@router.post("/security/alert-test", status_code=202)
async def security_alert_test(
    current_user: User = Depends(get_allowlisted_user(required_roles_any=[get_role("SystemAdministrator")])),  # noqa: B008
) -> dict:
    """Controlled test of the Security alert route (DIAAT-285 AC2, ALERTS.md).

    Records one synthetic security.ownership_violation with outcome "test": no record
    is touched, nothing about the caller is recorded. SystemAdministrator only, and
    404 (as for an unknown path) unless security_alert_test_allowed().
    """
    if not security_alert_test_allowed():
        raise HTTPException(status_code=404, detail="Not Found")
    record_ownership_violation(TRANSCRIPTION, READ, TEST)
    return {"event": "security.ownership_violation", "outcome": "test"}


@router.get("/healthcheck")
async def health_check_legacy():
    """Legacy endpoint for backwards compatibility"""
    return JSONResponse(status_code=200, content={"status": "ok", "version": APP_VERSION})


# The 404 of a template a jurisdiction does not use (not a 403: no access is refused).
TEMPLATE_NOT_AVAILABLE = "Not available in this jurisdiction"


def _require_decision_template(jurisdiction: ActiveJurisdiction) -> None:
    """404 unless the jurisdiction uses the IAC decision template (DIAAT-322): its
    form, document content and document. A jurisdiction without it dictates into the
    general section and copies its text out (DIAAT-333)."""
    if jurisdiction.settings.template != TEMPLATE_IAC_DECISION:
        raise HTTPException(status_code=404, detail=TEMPLATE_NOT_AVAILABLE)


@router.get("/document-content", response_model=DocumentContentResponse)
async def get_document_content_route(
    response: Response,
    current_user: User = Depends(get_allowlisted_user()),  # noqa: B008
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> DocumentContentResponse:
    """Return editable document content for allowlisted recorder users.

    It is the IAC decision template's (legal frameworks, hearing types): a jurisdiction
    without that template never sees it (DIAAT-330). It is the active jurisdiction's
    own legal wording (DIAAT-342); ``has_legal_wording`` is false when it has no legal
    framework, and the form then has no legal issues step.
    """
    response.headers["Cache-Control"] = "no-store"
    _require_decision_template(jurisdiction)
    data = get_document_content(jurisdiction.id)
    return DocumentContentResponse(
        hearing_types=data.get("hearing_types", []),
        legal_frameworks=data.get("legal_frameworks", []),
        anonymity=data.get("anonymity", {}),
        has_legal_wording=has_legal_wording(data),
    )


@router.get("/user/onboarding-status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(
    current_user: User = Depends(get_current_user),
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
        _sanitize_for_log(current_user.id),
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
    current_user: User = Depends(_ANY_APP_ROLE),
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> UploadUrlResponse:
    """Generate an upload URL for the client.

    When DIRECT_SDK_ACCESS is enabled, returns a short-lived Azure Blob SAS URL
    for direct browser-to-storage upload. Otherwise returns a backend proxy URL
    so the upload flows through the app domain.
    """
    settings = get_settings()
    # The file's UUID becomes the transcription ID: one journey ID (DIAAT-276) from
    # upload through transcription, minutes, document and notification.
    upload_id = uuid.uuid4()
    file_name = f"{upload_id}.{request.file_extension}"
    user_upload_blob_path = get_file_blob_path(str(current_user.azure_user_id), file_name)
    # DIAAT-322: the poller that makes the transcription has no request: it reads the
    # jurisdiction the upload was started in from here.
    await asyncio.to_thread(record_upload_jurisdiction, upload_id, jurisdiction.id)

    # The journey covers URL generation, so a failure here is correlated too; the
    # event is recorded only once the client really has a URL.
    with transcription_journey(upload_id):
        if settings.DIRECT_SDK_ACCESS:
            upload_url = await generate_blob_upload_url(
                container_name=settings.AZURE_STORAGE_CONTAINER_NAME,
                blob_name=user_upload_blob_path,
                expiry_hours=settings.UPLOAD_URL_TTL_MINUTES / 60,
            )
        else:
            encoded_key = urllib.parse.quote(user_upload_blob_path, safe="")
            upload_url = f"{settings.APP_URL.rstrip('/')}/api/upload-audio?key={encoded_key}"
        record_event("upload.url_issued", {"upload.method": "direct" if settings.DIRECT_SDK_ACCESS else "proxy"})

    return UploadUrlResponse(
        upload_url=upload_url,
        user_upload_s3_file_key=user_upload_blob_path,
    )


async def _verified_upload_transcription_id(key: str, current_user: User) -> str | None:
    """SEC-1: the file name's UUID becomes the transcription ID, and the key is client
    input. Refuse a key naming another user's transcription before storing anything.

    Early rejection only: the authoritative check is in the save itself
    (OwnershipConflictError), so a failed lookup here does not block the upload.
    Returns the key's transcription ID once verified (new, or the caller's own), so
    it may be used as a journey ID; otherwise None.
    """
    transcription_id = transcription_id_from_key(key)
    if transcription_id is None:
        return None  # not a UUID name: a new ID is generated on save
    try:
        owner = await asyncio.to_thread(get_transcription_owner, UUID(transcription_id))
    except Exception as exc:
        logger.warning("Upload key owner lookup failed (%s); relying on the save check", type(exc).__name__)
        return None
    if owner is not None and owner != current_user.id:
        logger.warning(
            "UNAUTHORISED_ACCESS_ATTEMPT user_id=%s reason=upload_key_names_other_users_transcription",
            _sanitize_for_log(current_user.id),
        )
        record_ownership_violation(TRANSCRIPTION, WRITE, actor_id=current_user.id, object_id=transcription_id)
        raise HTTPException(status_code=403, detail="Access denied")
    return transcription_id


@router.put("/upload-audio")
async def upload_audio_proxy(
    key: str,
    request: Request,
    current_user: User = Depends(_ANY_APP_ROLE),
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
):
    """Receive a raw audio upload and write it to blob storage.

    Used when DIRECT_SDK_ACCESS is disabled — the frontend PUTs the file here
    rather than directly to Azure Blob Storage.
    """
    # Validate the key and its owner BEFORE attaching any journey: the key is client
    # input, and a rejected request must not write events into another user's journey.
    # Rejections are client errors: recorded as a handled failure without a journey.
    # deferred_audit: a refusal's ACCESS_DENIED row is inserted off the event loop.
    try:
        async with deferred_audit():
            if not key.startswith("user-uploads/"):
                raise HTTPException(status_code=400, detail="Invalid upload key")
            _validate_blob_ownership(
                key, current_user, request, resource_type=AUDIO, action=WRITE, object_id=transcription_id_from_key(key)
            )
            # Only a verified ID (new, or the caller's own transcription) names the journey.
            transcription_id = await _verified_upload_transcription_id(key, current_user)
    except HTTPException as exc:
        code = "upload.forbidden" if exc.status_code == 403 else "upload.invalid_key"
        record_event("upload.rejected", {c.OUTCOME: "failure", c.ERROR_CODE: code})
        raise
    # DIAAT-334: a retry naming the caller's transcription of another jurisdiction (its
    # own security event, not a malformed key).
    if transcription_id is not None:
        await asyncio.to_thread(_refuse_other_jurisdiction, transcription_id, current_user, jurisdiction, AUDIO, WRITE)

    with (
        transcription_journey(transcription_id),
        stage("upload", attributes={"upload.method": "proxy"}) as upload_stage,
    ):
        body = await request.body()
        async with AsyncAzureBlobManager() as blob_manager:
            success = await blob_manager.create_blob_from_bytes(body, key)

        if not success:
            upload_stage.error("storage.write_failed")  # server-side: counts as an error
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
    current_user: User = Depends(_ANY_APP_ROLE),
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
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

    # Validate and get dialogue entries (raises HTTPException if invalid)
    # DIAAT-334: only a transcription of this request's jurisdiction.
    await asyncio.to_thread(
        _refuse_other_jurisdiction, request.transcription_id, current_user, jurisdiction, MINUTE_VERSION, WRITE
    )
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
        # DIAAT-276: minutes are part of the transcription's journey, and this
        # fire-and-forget task gets its own trace linked to this request (the request's
        # span ends as soon as the response is sent).
        with transcription_journey(request.transcription_id), background_span("Minutes Task", tracer_name=__name__):
            await _run_minutes_request()

    async def _run_minutes_request():
        if request.action_type == "generate":
            await generate_llm_output_task(
                dialogue_entries,
                request.transcription_id,
                request.template,
                str(user_id),
                minute_version_id=new_minute_version_id,
            )
        elif request.action_type == "edit":
            await ai_edit_task(
                dialogue_entries,
                request.current_minute_version_id,
                new_minute_version_id,
                request.edit_instructions,
                request.transcription_id,
                str(user_id),
            )

    asyncio.create_task(process_request())
    return JSONResponse(content={"minute_version_id": new_minute_version_id, "status": "initiated"})


@router.get("/templates", response_model=TemplateResponse)
async def get_templates(
    current_user: User = Depends(get_current_user),
):
    """Get all template categories (auth only, used during onboarding)."""
    templates = get_all_templates()
    return TemplateResponse(templates=templates)


@router.get("/transcriptions-metadata", response_model=list[TranscriptionMetadata])
async def get_transcriptions_metadata(
    current_user: User = Depends(_ANY_APP_ROLE),
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
    timezone: str = "Europe/London",  # Optional parameter with UK default
):
    """Get metadata for all transcriptions. Requires Judge, LegalTextManager, or SystemAdministrator role."""
    logger.info("getting transcription metadata for user %s", _sanitize_for_log(current_user.id))

    # Get timezone (fallback to UK if invalid)
    try:
        tz = pytz.timezone(timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = UK_TIMEZONE

    return fetch_transcriptions_metadata(current_user.id, tz, jurisdiction.id)


@router.get("/transcriptions/{transcription_id}", dependencies=[_in_jurisdiction(TRANSCRIPTION)], response_model=Transcription)
async def get_transcription(
    transcription_id: UUID,
    current_user: User = Depends(_ANY_APP_ROLE),
    timezone: str = "Europe/London",  # Optional parameter with UK default
):
    """Get a specific transcription by ID."""
    try:
        tz = pytz.timezone(timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = UK_TIMEZONE

    # DIAAT-286: audited in the database (audit_log), not logged.
    async with audited(
        TRANSCRIPT_VIEWED, actor_id=current_user.id, object_type=TRANSCRIPTION, object_id=transcription_id
    ) as audit:
        transcription = _owned_transcription(transcription_id, current_user, TRANSCRIPTION, tz)
        audit.authorised()
    return transcription


@router.post("/transcriptions", response_model=Transcription)
async def save_transcription_route(
    transcription_data: Transcription,
    current_user: User = Depends(_ANY_APP_ROLE),
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
):
    """Save or update a transcription."""
    logger.info("saving transcription for user %s", _sanitize_for_log(current_user.id))

    try:
        return save_transcription(transcription_data, current_user.id, jurisdiction.id)
    except JurisdictionConflictError:
        # DIAAT-334: the caller's record of another jurisdiction, as for an unknown ID.
        record_jurisdiction_blocked(
            TRANSCRIPTION, WRITE, actor_id=current_user.id, object_id=transcription_data.id
        )
        raise HTTPException(status_code=404, detail="Transcription not found") from None
    except OwnershipConflictError:
        # Someone else's transcription: same answer as for an unknown ID.
        raise HTTPException(status_code=404, detail="Transcription not found") from None


@router.get("/transcriptions/{transcription_id}/document-download", dependencies=[_in_jurisdiction(DOCUMENT)])
async def get_document_download_url(
    transcription_id: UUID,
    current_user: User = Depends(_ANY_APP_ROLE),
):
    """Generate a fresh, time-limited download URL for a hearing document.

    The document blob path is stored at submission time.  This endpoint
    generates a new SAS token on each request so the URL never expires
    before the user can click it.

    Audited as an export (DIAAT-286) with direct storage access: the browser fetches
    the document from storage with this link, so the link is the export. Without it,
    the link points at /api/download, which records the export itself; a refusal or
    failure here is still recorded.
    """
    async with audited(
        TRANSCRIPT_EXPORTED,
        actor_id=current_user.id,
        object_type=DOCUMENT,
        object_id=transcription_id,
        detail={"via": "link"},
    ) as audit:
        return await _document_download_url(transcription_id, current_user, audit)


async def _document_download_url(transcription_id: UUID, current_user: User, audit: AuditedAction) -> dict[str, str]:
    try:
        tz = UK_TIMEZONE
        transcription = get_transcription_by_id(transcription_id, current_user.id, tz)
    except Exception:
        raise HTTPException(status_code=404, detail="Transcription not found")
    audit.authorised()
    _assert_owned(transcription, current_user, DOCUMENT)

    # Get the blob path from the transcription job
    blob_path: str | None = None
    if transcription.transcription_jobs:
        blob_path = transcription.transcription_jobs[0].s3_audio_url

    if not blob_path:
        raise HTTPException(status_code=404, detail="No document found for this transcription")

    if blob_path.startswith("https://"):
        parsed = urllib.parse.urlparse(blob_path)
        path_parts = parsed.path.lstrip("/").split("/", 1)
        if len(path_parts) < 2:
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
        # The link only points at /api/download, which records the export when the
        # document is streamed: one row per download, not two.
        audit.skip_if_successful()

    logger.info(
        "Generated fresh download URL for transcription %s (document: %s)",
        str(transcription_id),
        _redact_blob_path(blob_path),
    )

    return {"download_url": download_url}


@router.get("/download", dependencies=[_in_jurisdiction(DOCUMENT)])
async def download_document(
    transcription_id: UUID,
    request: Request,
    current_user: User = Depends(_ANY_APP_ROLE),
):
    """Stream a transcript document from blob storage, verifying ownership before serving."""
    async with audited(
        TRANSCRIPT_EXPORTED,
        actor_id=current_user.id,
        object_type=DOCUMENT,
        object_id=transcription_id,
        detail={"via": "stream"},
    ) as audit:
        return await _stream_document(transcription_id, request, current_user, audit)


async def _stream_document(
    transcription_id: UUID, request: Request, current_user: User, audit: AuditedAction
) -> StreamingResponse:
    try:
        transcription = get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)
    except Exception:
        raise HTTPException(status_code=404, detail="Transcription not found")
    audit.authorised()
    _assert_owned(transcription, current_user, DOCUMENT)

    blob_path: str | None = None
    if transcription.transcription_jobs:
        blob_path = transcription.transcription_jobs[0].s3_audio_url

    if not blob_path:
        raise HTTPException(status_code=404, detail="No document found for this transcription")

    if blob_path.startswith("https://"):
        parsed = urllib.parse.urlparse(blob_path)
        path_parts = parsed.path.lstrip("/").split("/", 1)
        if len(path_parts) < 2:
            raise HTTPException(status_code=500, detail="Stored document URL is malformed")
        blob_path = path_parts[1]

    _validate_blob_ownership(blob_path, current_user, request, resource_type=DOCUMENT, object_id=transcription_id)

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
    try:
        blob_response = await client.send(blob_req, stream=True)
    except Exception:
        await client.aclose()
        raise  # audited as failed
    if blob_response.status_code >= 400:
        audit.fail("storage_status")

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

    # The export is recorded when the body has been sent (or failed), not now.
    return audit.streaming_response(generate(), status_code=blob_response.status_code, headers=response_headers)


@router.delete("/transcriptions/{transcription_id}", dependencies=[_in_jurisdiction(TRANSCRIPTION)], status_code=204)
async def delete_transcription(
    transcription_id: UUID,
    current_user: User = Depends(_ANY_APP_ROLE),
):
    """Delete a specific transcription by ID. Audited (DIAAT-286); a refusal (404:
    unknown or another user's) is ``denied``, an error ``failed``."""
    async with audited(TRANSCRIPT_DELETED, actor_id=current_user.id, object_type=TRANSCRIPTION, object_id=transcription_id):
        delete_transcription_by_id(transcription_id, current_user.id)


@router.get(
    "/transcriptions/{transcription_id}/minute-versions/{minute_version_id}", dependencies=[_in_jurisdiction(MINUTE_VERSION)],
    response_model=MinuteVersion,
)
async def get_minute_version_by_id_route(
    transcription_id: UUID,
    minute_version_id: UUID,
    current_user: User = Depends(_ANY_APP_ROLE),
):
    """Get a specific minute version by its ID for a given transcription."""
    # Verify user has access to the associated transcription
    _owned_transcription(transcription_id, current_user, MINUTE_VERSION)

    # Get the specific minute version, ensuring it belongs to the transcription
    minute_version = get_minute_version_by_id(minute_version_id, transcription_id)
    return minute_version


@router.get(
    "/transcriptions/{transcription_id}/minute-versions", dependencies=[_in_jurisdiction(MINUTE_VERSION)],
    response_model=list[MinuteVersion],
)
async def get_minute_versions_route(
    transcription_id: UUID,
    current_user: User = Depends(_ANY_APP_ROLE),
):
    """Get all minute versions for a specific transcription."""
    # Verify user has access to this transcription
    _owned_transcription(transcription_id, current_user, MINUTE_VERSION)

    return get_minute_versions(transcription_id)


@router.post("/transcriptions/{transcription_id}/minute-versions", dependencies=[_in_jurisdiction(MINUTE_VERSION)], response_model=MinuteVersion)
async def save_minute_version_route(
    transcription_id: UUID,
    minute_data: MinuteVersion,
    current_user: User = Depends(_ANY_APP_ROLE),
):
    """Create or update a minute version for a transcription."""
    transcription = get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)
    if not transcription:
        raise HTTPException(status_code=404, detail="Transcription not found")

    minute_data.transcription_id = transcription_id

    try:
        saved_minute = save_minute_version(minute_data, actor_id=current_user.id)
    except OwnershipConflictError:
        # The ID belongs to a minute version of another transcription.
        raise HTTPException(status_code=404, detail="Minute version not found") from None

    # No Langfuse "user-edit" event: it would send the full draft minutes (before
    # and after) to Langfuse on every manual edit. On main that code never ran (it
    # compared the content after saving), so no such data flow exists; whether to
    # add one is a product and privacy decision, not a bug fix.
    return saved_minute


@router.post("/transcriptions/{transcription_id}/jobs", dependencies=[_in_jurisdiction(TRANSCRIPTION_JOB)], response_model=TranscriptionJob)
async def save_transcription_job_route(
    transcription_id: UUID,
    job_data: TranscriptionJob,
    current_user: User = Depends(_ANY_APP_ROLE),
) -> TranscriptionJob:
    """Create a new transcription job for a transcription."""
    # Verify user has access to this transcription
    transcription = get_transcription_by_id(transcription_id, current_user.id, UK_TIMEZONE)
    if not transcription:
        raise HTTPException(status_code=404, detail="Transcription not found")

    # The job belongs to the transcription in the path (checked above), whatever the body says.
    job_data.transcription_id = transcription_id
    try:
        # Provenance is the server's: a client's value is ignored (DIAAT-286).
        job = save_transcription_job(job=job_data, from_client=True, actor_id=current_user.id)
    except OwnershipConflictError:
        raise HTTPException(status_code=404, detail="Transcription job not found") from None
    logger.info(
        "TRANSCRIPT_JOB_SUBMITTED transcription_id=%s job_id=%s user_id=%s",
        _sanitize_for_log(transcription_id),
        _sanitize_for_log(job.id),
        _sanitize_for_log(current_user.id),
    )
    return job


@router.get("/transcriptions/{transcription_id}/jobs", dependencies=[_in_jurisdiction(TRANSCRIPTION_JOB)], response_model=list[TranscriptionJob])
async def get_transcription_jobs_route(
    transcription_id: UUID,
    current_user: User = Depends(_ANY_APP_ROLE),
) -> list[TranscriptionJob]:
    """Get all transcription jobs for a specific transcription: the transcript text,
    so audited as a view of the transcript (DIAAT-286)."""
    async with audited(TRANSCRIPT_VIEWED, actor_id=current_user.id, object_type=TRANSCRIPT, object_id=transcription_id) as audit:
        # Verify user has access to this transcription
        _owned_transcription(transcription_id, current_user, TRANSCRIPTION_JOB)
        audit.authorised()
        return get_transcription_jobs(transcription_id)


@router.get("/transcriptions/{transcription_id}/jobs/{job_id}/audio", dependencies=[_in_jurisdiction(AUDIO)])
async def stream_audio(
    transcription_id: UUID,
    job_id: UUID,
    request: Request,
    current_user: User = Depends(_ANY_APP_ROLE),
):
    """Stream audio from blob storage with HTTP Range support for seekable playback.

    Audited as an export (DIAAT-286) once per playback: a request for the start of the
    file (no Range, or one from byte 0), recorded as a success even when the browser
    ends it early, as it does on a pause or a seek. Seeks, and a browser's probe of the
    first bytes (Safari asks for ``bytes=0-1``), are not recorded unless refused or failed.
    """
    async with audited(
        TRANSCRIPT_EXPORTED,
        actor_id=current_user.id,
        object_type=AUDIO,
        object_id=transcription_id,
        detail={"via": "stream", "job_id": str(job_id)},
    ) as audit:
        if not _starts_playback(request.headers.get("range")):
            audit.skip_if_successful()
        return await _stream_audio(transcription_id, job_id, request, current_user, audit)


# A request from byte 0 that ends before this asks only for the file's size or type.
_PLAYBACK_PROBE_MAX_END = 1023


def _starts_playback(range_header: str | None) -> bool:
    """Whether a request is the start of a playback: no Range, or one from byte 0
    that is not a probe of the first bytes."""
    if not range_header:
        return True
    # Bounded: int() refuses very long digit strings (Python's conversion limit), and
    # no real offset has more than 19 digits. Anything longer is not a playback start.
    match = re.fullmatch(r"bytes=(\d{1,19})-(\d{0,19})", range_header.replace(" ", "").split(",")[0])
    if match is None or int(match.group(1)) != 0:
        return False
    return not match.group(2) or int(match.group(2)) > _PLAYBACK_PROBE_MAX_END


async def _stream_audio(
    transcription_id: UUID, job_id: UUID, request: Request, current_user: User, audit: AuditedAction
) -> StreamingResponse:
    _owned_transcription(transcription_id, current_user, AUDIO)

    jobs = get_transcription_jobs(transcription_id)
    job = next((j for j in jobs if j.id == job_id), None)
    # Refused, not failed: another transcription's job, or none, is not found here.
    if not job:
        raise HTTPException(status_code=404, detail="Transcription job not found")
    if not job.s3_audio_url:
        raise HTTPException(status_code=404, detail="No audio available for this job")

    stored_url = job.s3_audio_url
    if stored_url.startswith("https://"):
        parsed = urllib.parse.urlparse(stored_url)
        path_parts = parsed.path.lstrip("/").split("/", 1)
        if len(path_parts) < 2:
            raise HTTPException(status_code=500, detail="Stored audio URL is malformed")
        blob_path = path_parts[1]
    else:
        blob_path = stored_url

    _validate_blob_ownership(blob_path, current_user, request, resource_type=AUDIO, object_id=transcription_id)
    audit.authorised()  # from here on, an error is a failure, not a refusal

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
    try:
        blob_response = await client.send(blob_req, stream=True)
    except Exception:
        await client.aclose()
        raise  # audited as failed
    if blob_response.status_code >= 400:
        audit.fail("storage_status")

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

    # The export is recorded when the body has been sent (or failed), not now.
    return audit.streaming_response(
        generate(), status_code=blob_response.status_code, headers=response_headers, client_may_stop=True
    )


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
    """Request model that accepts either sectioned or flat message formats.

    Either the IAC decision's ``form_data``, or (DIAAT-344) an uploaded template's
    ``template_id`` and the judge's ``values`` for its fields, by box tag.
    """

    model_config = ConfigDict(extra="forbid")

    form_data: LiveTranscriptionFormRequest | None = None
    template_id: uuid.UUID | None = None
    values: dict[str, str | bool | list[str] | None] | None = Field(default=None, max_length=500)
    messages: SectionedMessages | list[LiveTranscriptMessage]
    notes: str | None = Field(default=None, max_length=200_000)

    @model_validator(mode="after")
    def _one_form(self) -> "LiveTranscriptionSubmissionRequest":
        if (self.form_data is None) == (self.template_id is None):
            msg = "Send either form_data, or template_id with values."
            raise ValueError(msg)
        if (self.template_id is None) != (self.values is None):
            msg = "Send template_id and values together."
            raise ValueError(msg)
        return self

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
    "/transcriptions/{transcription_id}/sectioned-transcript", dependencies=[_in_jurisdiction(TRANSCRIPTION)],
    response_model=TranscriptViewerResponse,
)
async def get_sectioned_transcript_route(
    transcription_id: UUID,
    current_user: User = Depends(_ANY_APP_ROLE),
) -> TranscriptViewerResponse:
    """Get the recorder-style sectioned transcript for a hearing if available.

    Audited as a view of the transcript (DIAAT-286)."""
    async with audited(
        TRANSCRIPT_VIEWED, actor_id=current_user.id, object_type=SECTIONED_TRANSCRIPT, object_id=transcription_id
    ) as audit:
        return await _sectioned_transcript(transcription_id, current_user, audit)


async def _sectioned_transcript(
    transcription_id: UUID, current_user: User, audit: AuditedAction
) -> TranscriptViewerResponse:
    _owned_transcription(transcription_id, current_user, TRANSCRIPTION)
    audit.authorised()

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

        latest_blob = max(
            matching_blobs,
            key=lambda blob: blob.get("last_modified") or datetime.min.replace(tzinfo=UTC),
        )

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


class _RenderHandoff:
    """Who owns a rendered hearing document: the request, or nobody.

    A worker thread cannot be stopped. Whichever of the worker (finishing) and the
    request (being cancelled) comes second deletes the document, under the lock,
    so it is cleaned up even when the event loop is cancelled or closed at
    shutdown and no asyncio callback would run.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.abandoned = False
        self.path: Path | None = None

    def render(self, renderer=None, **kwargs) -> Path:
        path = (renderer or render_hearing_document)(**kwargs)
        with self.lock:
            if self.abandoned:
                path.unlink(missing_ok=True)
            else:
                self.path = path
        return path

    def abandon(self) -> None:
        with self.lock:
            self.abandoned = True
            if self.path is not None:
                # Rendered, but the request was cancelled before it received the path.
                self.path.unlink(missing_ok=True)


async def _render_hearing_document_off_loop(**kwargs) -> Path:
    """Render the hearing document in a worker thread, leaving no file behind on cancellation.

    If the request is cancelled while rendering (including at shutdown), the caller's
    cleanup never learns the output path, so the document (transcript and judicial
    notes) is deleted by whichever side finishes last; see _RenderHandoff.
    """
    handoff = _RenderHandoff()
    try:
        return await asyncio.to_thread(handoff.render, **kwargs)
    except asyncio.CancelledError:
        handoff.abandon()
        raise


@router.post("/live-transcription/submission", response_model=LiveTranscriptionSubmissionResponse)
async def save_live_transcription_submission(
    request: LiveTranscriptionSubmissionRequest,
    current_user: User = Depends(_ANY_APP_ROLE),
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
    dictation_session_id: str | None = Header(default=None, alias="X-Dictation-Session-Id"),
) -> LiveTranscriptionSubmissionResponse:
    """Persist live transcription form and transcript data as JSON in the audio uploads bucket.

    The document is the IAC decision (``form_data``) or, DIAAT-344, one of the
    jurisdiction's uploaded templates filled with the judge's ``values``."""
    if request.template_id is None:
        _require_decision_template(jurisdiction)
        plan = None
    else:
        plan = await asyncio.to_thread(_uploaded_template_plan, request, jurisdiction)
    # DIAAT-276: the submission is a journey; the transcription it creates is related
    # on the transcript.available event.
    submission_id = uuid.uuid4()
    # The browser's dictation session (OBS-4) is the journey's parent, so every event
    # of the submission (stages, document, transcript) carries it as
    # hmcts.journey.parent_id. Only a UUID is accepted; anything else is ignored.
    # The value is client-asserted (as the session journey itself is: the browser
    # makes its ID with crypto.randomUUID). It is kept as the parent for DIAAT-279
    # traceability: one query by session ID finds the submission. A user can only
    # name a session whose ID they hold; another user's session ID is random and
    # is never returned by the API, and the relay drops browser-sent parents.
    with journey(JOURNEY_SUBMISSION, str(submission_id), parent_id=_dictation_session_id(dictation_session_id)):
        try:
            # The stage sees the original exception (one exception event, its real
            # type as the code); it is turned into the client's generic 500 outside.
            with stage("submission") as submission_stage:
                return await _save_live_transcription_submission(
                    request, current_user, jurisdiction, submission_id, submission_stage, plan
                )
        except HTTPException:
            raise
        except Exception:
            logger.exception(
                "Error saving live transcription submission for user %s",
                _sanitize_for_log(current_user.id),
            )
            raise HTTPException(status_code=500, detail="Failed to save submission data") from None


@dataclass
class _DocumentPlan:
    """How a submission's document is made and named: the IAC decision, or (DIAAT-344)
    an uploaded template."""

    template: str
    render_kwargs: dict[str, Any]
    # What the stored submission records of the form.
    form_record: dict[str, Any]
    title: str
    download_filename: str
    # An uploaded template's: its file's hash. The decision's comes from the render.
    version: str | None = None


def _filename_part(name: str) -> str:
    """Characters safe in a file name only."""
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()


_HEARING_DOCUMENTS_DIR = Path(tempfile.gettempdir()) / "hearing_documents"


def _decision_plan(
    request: LiveTranscriptionSubmissionRequest, current_user: User, jurisdiction: ActiveJurisdiction
) -> _DocumentPlan:
    form = request.form_data
    assert form is not None  # checked by the request model
    # Format: "[{date}] {Appellant} v {Respondent}.docx"
    return _DocumentPlan(
        template=DECISION_TEMPLATE,
        render_kwargs={
            "request": request,
            "output_dir": _HEARING_DOCUMENTS_DIR,
            "user_email": current_user.email,
            # DIAAT-342: the active jurisdiction's legal wording, not the form's
            # free-text jurisdiction.
            "jurisdiction_id": jurisdiction.id,
        },
        form_record=form.model_dump(by_alias=True, exclude_none=True),
        title=f"{form.appellant_name} v {form.respondent}",
        download_filename=f"[{form.hearing_date}] {_filename_part(form.appellant_name)} v {_filename_part(form.respondent)}.docx",
    )


def _render_uploaded_template(*, content: bytes, texts: dict[str, str], output_dir: Path) -> Path:
    return fill_template(content, texts, output_dir / f"hearing-document-{uuid.uuid4()}.docx")


def _uploaded_template_plan(
    request: LiveTranscriptionSubmissionRequest, jurisdiction: ActiveJurisdiction
) -> _DocumentPlan:
    """The judge's chosen template, filled with their values: 404 when the jurisdiction's
    judges cannot choose it, 400 (``template_values_invalid``, ``problems``) for values
    that do not fit its fields."""
    assert request.template_id is not None
    with Session(get_engine()) as session:
        row = jurisdiction_templates.available_template(session, jurisdiction.id, request.template_id)
        if row is None:
            raise HTTPException(status_code=404, detail="This template is not available in this jurisdiction.")
        fields = jurisdiction_templates.fields_of(row)
        content, sha256, name = row.content, row.sha256, row.name
    legal_issue_titles: dict[str, str] = {}
    if any(f.type == "legal_issues" for f in fields):
        frameworks = get_document_content(jurisdiction.id).get("legal_frameworks") or []
        legal_issue_titles = {
            str(f["id"]): str(f.get("title") or f["id"])
            for f in sorted(frameworks, key=lambda f: f.get("rank", 0))
            if f.get("id")
        }
    values, problems = check_values(fields, request.values or {}, list(legal_issue_titles))
    if problems:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "template_values_invalid",
                "message": "The hearing details do not fit the template. " + " ".join(problems),
                "problems": problems,
            },
        )
    transcript = [m.text for m in request.get_sectioned_messages().get_all_messages()]
    texts = box_text(
        fields,
        values,
        FillContext(
            jurisdiction_name=jurisdiction.current.shown_name,
            year=datetime.now(UTC).year,
            dictated_text=dictated_text(transcript, request.notes),
            legal_issue_titles=legal_issue_titles,
        ),
    )
    # The first text the judge gave (a case reference, say) tells their hearings apart.
    first_text = next(
        (str(values[f.tag]) for f in fields if f.type == "text" and isinstance(values.get(f.tag), str)), None
    )
    today = datetime.now(UTC).date().isoformat()
    return _DocumentPlan(
        template="uploaded",
        render_kwargs={
            "renderer": _render_uploaded_template,
            "content": content,
            "texts": texts,
            "output_dir": _HEARING_DOCUMENTS_DIR,
        },
        form_record={"template_id": str(request.template_id), "template_name": name, "values": values},
        title=f"{name}: {first_text}" if first_text else name,
        download_filename=f"[{today}] {_filename_part(name) or 'Document'}.docx",
        version=sha256[:12],
    )


def _dictation_session_id(value: str | None) -> str | None:
    """The X-Dictation-Session-Id header as a canonical UUID, or None."""
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


async def _save_live_transcription_submission(  # NOSONAR
    request: LiveTranscriptionSubmissionRequest,
    current_user: User,
    jurisdiction: ActiveJurisdiction,
    submission_id: uuid.UUID,
    submission_stage: StageHandle,
    plan: _DocumentPlan | None = None,
) -> LiveTranscriptionSubmissionResponse:
    plan = plan or _decision_plan(request, current_user, jurisdiction)
    # DIAAT-279: on the document events; the version is added once rendering has
    # loaded the document content: the content of this render only (DIAAT-286, see
    # documents/versioning.py), also stored as the document's provenance.
    document_attributes: dict[str, str] = {"document.template": plan.template}
    blob_filename = f"live-transcription-submission-{submission_id}.json"
    blob_path = get_file_blob_path(str(current_user.azure_user_id), blob_filename)

    # Get sectioned messages (handles both sectioned and legacy flat formats)
    sectioned_messages = request.get_sectioned_messages()

    # Session data payload on "Submit hearing" button click
    # Store messages in sectioned format for document generation
    payload = {
        "form_data": plan.form_record,
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

    logger.info(
        "live transcription submission received submission_id=%s user_id=%s",
        _sanitize_for_log(submission_id),
        _sanitize_for_log(current_user.id),
    )

    temp_json_path: Path | None = None
    temp_doc_path: Path | None = None

    try:
        # Save JSON payload to blob storage
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as tmp_file:
            temp_json_path = Path(tmp_file.name)
            json.dump(payload, tmp_file)

        async with AsyncAzureBlobManager() as blob_manager:
            upload_success = await blob_manager.create_blob_from_file(temp_json_path, blob_path)

            if not upload_success:
                submission_stage.error("storage.write_failed")  # server-side: counts as an error
                raise HTTPException(status_code=500, detail="Failed to save submission data")

            logger.info(
                "Live transcription submission saved to blob %s for user %s",
                _redact_blob_path(blob_path),
                _sanitize_for_log(current_user.id),
            )

            # Generate the Word document and store it. DIAAT-279: one stage for both,
            # so a document that cannot be stored is a failed generation, with the
            # template, its version and a code saying where it failed.
            with stage("document_generation", attributes=document_attributes) as document_stage:
                with track_render() as render:
                    try:
                        # Off the event loop; no file left behind on cancellation.
                        temp_doc_path = await _render_hearing_document_off_loop(**plan.render_kwargs)
                    except Exception:
                        document_stage.set_attribute("document.template_version", plan.version or render.version)
                        document_stage.error("document.render_failed")
                        raise
                generated_at = datetime.now(UTC)
                document_attributes["document.template_version"] = plan.version or render.version
                document_stage.set_attribute(
                    "document.template_version", document_attributes["document.template_version"]
                )
                # DIAAT-286: stored with the document's job record, in the same insert.
                provenance = decision_document_provenance(
                    template=plan.template,
                    template_version=document_attributes["document.template_version"],
                    generated_at=generated_at,
                ).to_record()

                logger.info(
                    "Rendered hearing document for user %s",
                    _sanitize_for_log(current_user.id),
                )

                # Upload the Word document to blob storage
                doc_blob_filename = f"hearing-document-{submission_id}.docx"
                doc_blob_path = get_file_blob_path(str(current_user.azure_user_id), doc_blob_filename)

                try:
                    doc_upload_success = await blob_manager.create_blob_from_file(temp_doc_path, doc_blob_path)
                except Exception:  # raised rather than returned False: still not stored
                    document_stage.error("document.store_failed")
                    submission_stage.error("document.store_failed")
                    raise
                if not doc_upload_success:
                    # Generated but not stored: distinct from the submission JSON's
                    # storage.write_failed, on both the document and submission stages.
                    document_stage.error("document.store_failed")
                    submission_stage.error("document.store_failed")
                    raise HTTPException(status_code=500, detail="Failed to upload document to storage")

                logger.info(
                    "Uploaded hearing document to blob %s for user %s",
                    _redact_blob_path(doc_blob_path),
                    _sanitize_for_log(current_user.id),
                )

        # A user-friendly download filename (see _DocumentPlan)
        download_filename = plan.download_filename

        # Create transcription record for the hearing
        transcription_title = plan.title
        transcription = Transcription(
            user_id=current_user.id,
            title=transcription_title,
        )
        saved_transcription = save_transcription(transcription, current_user.id, jurisdiction.id)

        # Generate download URL for the document
        _settings = get_settings()
        if _settings.DIRECT_SDK_ACCESS:
            doc_download_url = await generate_blob_download_url(
                container_name=_settings.AZURE_STORAGE_CONTAINER_NAME,
                blob_name=doc_blob_path,
                expiry_hours=_settings.DOWNLOAD_URL_TTL_MINUTES / 60,
                download_filename=download_filename,
            )
            # The download filename is built from the party names and hearing
            # date, so it is case-identifying and must not be logged.
            logger.info(
                "Generated download URL for hearing document %s (expires in %d minutes)",
                _redact_blob_path(doc_blob_path),
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
            provenance=provenance,
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
                _redact_blob_path(blob_path),
            )

        logger.info(
            "Created transcription record %s for hearing submission",
            str(saved_transcription.id),
        )
        # Only now is the document reachable by the user (stored, its job saved and a
        # download link issued), and the transcription usable.
        record_event("document.available", document_attributes)
        record_event("transcript.available", {"dictation.transcription_id": str(saved_transcription.id)})

        return LiveTranscriptionSubmissionResponse(
            blob_path=blob_path,
            document_blob_path=doc_blob_path,
            document_download_url=doc_download_url,
            transcription_id=str(saved_transcription.id),
            message="Submission data saved and document generated",
        )
    finally:
        # Clean up temporary files
        if temp_json_path and temp_json_path.exists():
            temp_json_path.unlink(missing_ok=True)
        if temp_doc_path and temp_doc_path.exists():
            temp_doc_path.unlink(missing_ok=True)


class AddTagRequest(BaseModel):
    name: str


@router.get("/transcriptions/{transcription_id}/tags", dependencies=[_in_jurisdiction(TRANSCRIPTION)], response_model=list[Tag])
async def get_transcription_tags_route(
    transcription_id: UUID,
    current_user: User = Depends(_ANY_APP_ROLE),
) -> list[Tag]:
    """Get all tags for a specific transcription."""
    return get_tags_for_transcription(transcription_id, current_user.id)


@router.post("/transcriptions/{transcription_id}/tags", dependencies=[_in_jurisdiction(TRANSCRIPTION)], response_model=Tag)
async def add_tag_to_transcription_route(
    transcription_id: UUID,
    tag_request: AddTagRequest,
    current_user: User = Depends(_ANY_APP_ROLE),
) -> Tag:
    """Add a tag to a transcription."""
    return add_tag_to_transcription(transcription_id, tag_request.name, current_user.id)


@router.delete("/transcriptions/{transcription_id}/tags/{tag_name}", dependencies=[_in_jurisdiction(TRANSCRIPTION)], status_code=204)
async def remove_tag_from_transcription_route(
    transcription_id: UUID,
    tag_name: str,
    current_user: User = Depends(_ANY_APP_ROLE),
):
    """Remove a tag from a transcription."""
    remove_tag_from_transcription(transcription_id, tag_name, current_user.id)


@router.get("/user", response_model=User)
async def get_current_user_route(
    current_user: User = Depends(get_current_user),
):
    """Get the current user's details (auth only, no allowlist check)."""
    return get_user_by_id(current_user.id)


# Add missing routes that frontend expects
@router.get("/users/me", response_model=User)
async def get_current_user_me_route(
    current_user: User = Depends(get_current_user),
):
    """Get the current user's details (auth only, no allowlist check)."""
    return get_user_by_id(current_user.id)


@router.get("/user/profile", response_model=User)
async def get_user_profile_route(
    current_user: User = Depends(get_current_user),
):
    """Get the current user's profile (auth only, no allowlist check)."""
    return get_user_by_id(current_user.id)


@router.post("/user", response_model=User)  # changed from .patch to .post
async def update_current_user_route(
    request: UpdateUserRequest,
    current_user: User = Depends(get_current_user),
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
    trace_id = langfuse_trace_id(request.trace_id)
    if trace_id is None:
        # Langfuse v3+ cannot attach an event to a v2 (UUID) trace; without a trace
        # context the event would land in this request's trace instead.
        raise HTTPException(status_code=400, detail="Trace id is not a Langfuse v3+ trace id")
    try:
        # The user goes in the event's metadata: propagate_attributes would also set it
        # on the current span, here the request span that App Insights exports.
        langfuse_client.create_event(
            trace_context={"trace_id": trace_id},
            name=request.name,
            metadata={**(request.metadata or {}), "user_id": str(current_user.id)},
            input=request.input_data,
            output=request.output_data,
        )

        logger.info(
            "Langfuse trace '%s' submitted for trace %s by user %s",
            _sanitize_for_log(request.name),
            _sanitize_for_log(request.trace_id),
            _sanitize_for_log(current_user.id),
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
        langfuse_client.create_score(
            trace_id=request.trace_id,
            name=request.name,
            value=request.value,
            comment=request.comment,
        )

        logger.info(
            "Langfuse score submitted for trace %s by user %s",
            _sanitize_for_log(request.trace_id),
            _sanitize_for_log(current_user.id),
        )

    except Exception as e:
        _e = f"Failed to submit Langfuse score: {e!s}"
        logger.error(_e)
        raise HTTPException(status_code=500, detail=_e) from e
    else:
        return {"success": True, "message": "Score submitted successfully"}


@router.get("/get-speech-token")
async def get_speech_token(
    current_user: User = Depends(_ANY_APP_ROLE),
):
    """
    Get Azure Speech Services token for real-time transcription.

    This endpoint fetches a temporary token from Azure Speech Services
    that can be used by the frontend for real-time speech recognition.
    Tokens are valid for 10 minutes.
    """
    settings = get_settings()

    # OBS-4: token-mint latency and failures are an SLI for starting dictation.
    with stage("speech_token") as token_stage:
        # Configuration first, in one place (same responses as before).
        if not settings.AZURE_SPEECH_ENDPOINT or not (settings.AZURE_SPEECH_RESOURCE_ID or settings.AZURE_SPEECH_KEY):
            logger.error("Azure Speech credentials not configured")
            token_stage.error("speech_token.not_configured")
            detail = (
                "Azure Speech Services not configured"
                if not settings.AZURE_SPEECH_ENDPOINT
                else "Azure Speech Services not configured: set AZURE_SPEECH_KEY or AZURE_SPEECH_RESOURCE_ID"
            )
            raise HTTPException(status_code=500, detail=detail)

        try:
            if settings.AZURE_SPEECH_RESOURCE_ID:
                # Managed Identity path — required when public network access is disabled.
                # Returns an AAD token in the format the Speech SDK expects for Entra auth.
                from azure.identity.aio import DefaultAzureCredential
                async with DefaultAzureCredential() as credential:
                    token_response = await credential.get_token("https://cognitiveservices.azure.com/.default")
                token = f"aad#{settings.AZURE_SPEECH_RESOURCE_ID}#{token_response.token}"
                logger.info("Speech token (AAD/Managed Identity) generated for user %s", _sanitize_for_log(current_user.id))
            else:
                # API key STS path — requires public network access to the Speech endpoint.
                fetch_token_url = f"{settings.AZURE_SPEECH_ENDPOINT.rstrip('/')}/sts/v1.0/issueToken"
                headers = {"Ocp-Apim-Subscription-Key": settings.AZURE_SPEECH_KEY}
                async with httpx.AsyncClient() as client:
                    response = await client.post(fetch_token_url, headers=headers)
                    response.raise_for_status()
                token = response.text
                logger.info("Speech token (API key/STS) generated for user %s", _sanitize_for_log(current_user.id))
        except Exception as e:
            token_stage.error("speech_token.request_failed")
            # Type only, to the log and the client: the error text can include the
            # endpoint, the response body or credential details.
            logger.error("Failed to fetch speech token (%s)", type(e).__name__)
            raise HTTPException(status_code=500, detail="Failed to fetch speech token") from e

    endpoint = settings.AZURE_SPEECH_ENDPOINT if settings.DIRECT_SDK_ACCESS else settings.APP_URL
    return {"token": token, "endpoint": endpoint}


@router.post("/generate-realtime-summary")
async def generate_realtime_summary_endpoint(
    request: GenerateSummaryRequest,
    current_user: User = Depends(_ANY_APP_ROLE),
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
            _sanitize_for_log(current_user.id),
            len(request.transcript_entries),
        )

        # Convert Pydantic models to dicts for the LLM function
        transcript_dicts = [entry.model_dump() for entry in request.transcript_entries]

        # Call the LLM to generate summary
        summary = await generate_realtime_summary(
            transcript_entries=transcript_dicts, existing_summary=request.existing_summary
        )

        logger.info("Realtime summary generated successfully for user %s", _sanitize_for_log(current_user.id))

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
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> LiveDraftResponse:
    """Return the current user's in-progress draft in this jurisdiction, or 404 if none or expired."""
    draft = get_live_draft(current_user.id, jurisdiction.id)
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
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> None:
    """Create or overwrite the current user's in-progress draft in this jurisdiction."""
    upsert_live_draft(current_user.id, jurisdiction.id, payload.transcript, payload.form_data)


@router.delete("/live-draft", status_code=204)
async def delete_live_draft_route(
    current_user: User = Depends(get_allowlisted_user()),  # noqa: B008
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> None:
    """Delete the current user's in-progress draft in this jurisdiction (on restore, discard, or submit)."""
    delete_live_draft(current_user.id, jurisdiction.id)
