"""Admin API routes for managing document content (legal texts).

All endpoints require authenticated users to pass JWT role checks.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from hmcts_azure_auth.audit import AuditEvent, AuditEventType
from pydantic import BaseModel, StringConstraints
from pydantic import Field as PydanticField
from sqlmodel import Field, Session

from transcribe_api import jurisdiction_templates, jurisdictions_admin
from transcribe_api.api.document_content_models import (
    AdminDocumentContentResponse,
    AnonymityStatusPayload,
    HearingTypePayload,
    JurisdictionRef,
    LegalFrameworkPayload,
)
from transcribe_api.api.routes_jurisdictions import AvailableTemplate, template_forms
from transcribe_api.documents.content_loader import get_document_content as load_document_content
from transcribe_api.documents.content_loader import has_legal_wording, save_document_content
from transcribe_api.documents.minutes.types import AdminTranscriptionListResponse, AdminTranscriptionRecord
from transcribe_api.documents.uploaded_templates import MAX_TEMPLATE_BYTES, TemplateField
from transcribe_api.domain.audit import (
    TRANSCRIPT_EXPORTED,
    TRANSCRIPT_VIEWED,
    USER_DELETED,
    audited,
    record_audit,
    write_audit_event,
)
from transcribe_api.domain.auth.approles_dictation import get_role, get_valid_roles
from transcribe_api.domain.auth.dependencies_dictation import (
    get_active_jurisdiction,
    get_allowlisted_user,
    get_current_user,
)
from transcribe_api.domain.exceptions import UserHasTranscriptionsError, UserNotFoundError
from transcribe_api.domain.interface_dictation import (
    admin_delete_user,
    admin_fetch_all_transcriptions,
    admin_get_transcription_by_id,
    admin_list_users,
    get_user_by_id,
    update_user,
)
from transcribe_api.domain.jurisdictions import (
    IAC_JURISDICTION_ID,
    ROLE_ADMIN,
    TEMPLATE_IAC_DECISION,
    WORDING_KEYS,
    ActiveJurisdiction,
)
from transcribe_api.domain.models_dictation import BaseTable, Transcription
from transcribe_api.runtime.db_connection import get_engine
from transcribe_api.runtime.log_sanitization import sanitize_for_log
from transcribe_api.runtime.security_events import DOCUMENT, READ, TRANSCRIPTION, record_jurisdiction_blocked
from transcribe_api.runtime.settings_dictation import get_settings
from transcribe_api.stt.utils import generate_blob_download_url

if TYPE_CHECKING:
    from transcribe_api.domain.audit import AuditedAction
    from transcribe_api.domain.models_dictation import User

logger = logging.getLogger(__name__)

def _admin_refuse_other_jurisdiction(
    transcription: Transcription, admin_user: User, jurisdiction: ActiveJurisdiction, resource_type: str, action: str
) -> None:
    """DIAAT-334: an admin sees any judge's material, but only in the jurisdiction they
    are working in; another's is the 404 of an unknown record, and recorded."""
    if transcription.jurisdiction_id != jurisdiction.id:
        record_jurisdiction_blocked(resource_type, action, actor_id=admin_user.id, object_id=transcription.id)
        raise HTTPException(status_code=404, detail="Transcription not found")


router = APIRouter(prefix="/admin", tags=["admin"])


class AdminUserResponse(BaseModel):
    email: str
    role: str = "admin"
    user_id: str
    is_admin: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_content(jurisdiction_id: UUID) -> dict[str, Any]:
    """A copy of the jurisdiction's legal wording, for the route to change and write."""
    try:
        return copy.deepcopy(load_document_content(jurisdiction_id))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unable to load document content") from exc


def _write_content(data: dict[str, Any], jurisdiction_id: UUID) -> None:
    try:
        save_document_content(jurisdiction_id, data)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to persist document content: {exc}") from exc


def _log_admin_content_change(*, admin_id: str, action: str, item_id: str) -> None:
    logger.info(
        "Admin admin_id=%s %s '%s'",
        sanitize_for_log(admin_id),
        action,
        sanitize_for_log(item_id),
    )


def _record_content_change(scope: LegalWordingScope, action: str, item_id: str) -> None:
    """Log and audit a legal wording change: which jurisdiction, what kind of change and
    which item, never the wording."""
    _log_admin_content_change(admin_id=str(scope.user.id), action=action, item_id=item_id)
    record_audit(
        "jurisdiction.edit",
        actor_id=scope.user.id,
        outcome="success",
        object_type="jurisdiction",
        detail={"code": scope.jurisdiction.code, "fields": ["legal_wording"], "change": action, "item": item_id},
    )


def _require_role(name: str) -> str:
    """Return the configured value for a named role.

    Raises ValueError at startup if the role is absent from AUTH_APPROLES,
    so a misconfigured deployment fails immediately rather than silently
    denying all users at runtime.
    """
    role = get_role(name)
    if role is None:
        raise ValueError(
            f"Role '{name}' is not configured. Check the AUTH_APPROLES environment variable."
        )
    return role


# ---------------------------------------------------------------------------
# Admin identity
# ---------------------------------------------------------------------------


@router.get("/me", response_model=AdminUserResponse)
async def admin_me(
    admin_user: User = Depends(get_allowlisted_user(required_roles_any=[_require_role("LegalTextManager"), _require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> AdminUserResponse:
    """Return the admin user's identity and their highest admin role.

    role is SystemAdministrator when the user holds that role, otherwise
    LegalTextManager (the only other role that reaches this endpoint).
    """
    app_roles: list[str] = admin_user.app_roles
    if _require_role("SystemAdministrator") in app_roles:
        admin_role = _require_role("SystemAdministrator")
    else:
        admin_role = _require_role("LegalTextManager")
    return AdminUserResponse(
        email=admin_user.email,
        role=admin_role,
        user_id=str(admin_user.id),
        is_admin=True,
    )


@router.get("/roles", response_model=list[str])
async def get_admin_roles(
    admin_user: User = Depends(get_allowlisted_user(required_roles_any=[_require_role("LegalTextManager"), _require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> list[str]:
    return list(get_valid_roles().values())


# ---------------------------------------------------------------------------
# Legal wording access (DIAAT-342)
#
# The routes below edit the legal wording (hearing types, legal frameworks, anonymity
# text) of ONE jurisdiction: the one the request works in (X-Jurisdiction, as for every
# route: a jurisdiction the user does not belong to is 403 jurisdiction_not_available).
# Who may:
#   - an admin member of that jurisdiction (any BenchNotes app role): read and write;
#   - for the IAC only, as before jurisdictions: the global LegalTextManager role (read
#     and write) and SystemAdministrator (read only).
# A jurisdiction without the IAC decision template has no legal wording to edit (404, as
# for its judges).
# ---------------------------------------------------------------------------

_LEGAL_WORDING_ADMIN_REQUIRED = "jurisdiction_admin_required"


@dataclass(frozen=True)
class LegalWordingScope:
    """Who is editing which jurisdiction's legal wording."""

    user: User
    jurisdiction: ActiveJurisdiction


def _may_edit_legal_wording(roles: list[str], jurisdiction: ActiveJurisdiction, *, write: bool) -> bool:
    ltm, sysadmin = _require_role("LegalTextManager"), _require_role("SystemAdministrator")
    has_app_role = any(r in roles for r in (_require_role("Normal"), _require_role("Judge"), ltm, sysadmin))
    if jurisdiction.current.role == ROLE_ADMIN and has_app_role:
        return True
    if jurisdiction.id == IAC_JURISDICTION_ID:
        return ltm in roles or (not write and sysadmin in roles)
    return False


def _legal_wording_access(*, write: bool):
    async def check(
        request: Request,
        response: Response,
        current_user: User = Depends(get_current_user),
        jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
    ) -> LegalWordingScope:
        response.headers["Cache-Control"] = "no-store"
        roles: list[str] = current_user.__dict__.get("app_roles", [])
        if not _may_edit_legal_wording(roles, jurisdiction, write=write):
            required = [f"admin of {jurisdiction.code}"]
            if jurisdiction.id == IAC_JURISDICTION_ID:
                required.append(_require_role("LegalTextManager"))
            await asyncio.to_thread(
                write_audit_event,
                AuditEvent(
                    event_type=AuditEventType.ACCESS_DENIED,
                    user_id=sanitize_for_log(getattr(current_user, "azure_user_id", "unknown")),
                    email=sanitize_for_log(getattr(current_user, "email", "unknown")),
                    held_roles=[sanitize_for_log(r) for r in roles],
                    required_roles=required,
                    resource=sanitize_for_log(f"{request.method} {request.url.path}"),
                    client_ip=request.client.host if request.client else None,
                ),
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "code": _LEGAL_WORDING_ADMIN_REQUIRED,
                    "message": f"Only an admin for {jurisdiction.code} can change its legal wording.",
                },
            )
        if jurisdiction.settings.template != TEMPLATE_IAC_DECISION:
            raise HTTPException(status_code=404, detail="Not available in this jurisdiction")
        return LegalWordingScope(user=current_user, jurisdiction=jurisdiction)

    return check


_LEGAL_WORDING_READ = _legal_wording_access(write=False)
_LEGAL_WORDING_WRITE = _legal_wording_access(write=True)


# ---------------------------------------------------------------------------
# Read all
# ---------------------------------------------------------------------------


@router.get("/document-content", response_model=AdminDocumentContentResponse)
async def get_document_content(
    scope: LegalWordingScope = Depends(_LEGAL_WORDING_READ),
) -> AdminDocumentContentResponse:
    """Return the legal wording of the jurisdiction the admin is working in (and only
    that one), with ``has_legal_wording`` and which jurisdiction it is."""
    data = _read_content(scope.jurisdiction.id)
    return AdminDocumentContentResponse(
        hearing_types=data.get("hearing_types", []),
        legal_frameworks=data.get("legal_frameworks", []),
        anonymity=data.get("anonymity", {}),
        has_legal_wording=has_legal_wording(data),
        jurisdiction=JurisdictionRef(code=scope.jurisdiction.code, name=scope.jurisdiction.current.name),
    )


# ---------------------------------------------------------------------------
# Hearing types
# ---------------------------------------------------------------------------


@router.put("/document-content/hearing-types/{item_id}")
async def update_hearing_type(
    item_id: str,
    payload: HearingTypePayload,
    scope: LegalWordingScope = Depends(_LEGAL_WORDING_WRITE),
) -> dict[str, Any]:
    """Update an existing hearing type by ID."""
    data = _read_content(scope.jurisdiction.id)
    items: list[dict] = data.get("hearing_types", [])
    for i, item in enumerate(items):
        if item.get("id") == item_id:
            items[i] = payload.model_dump()
            _write_content(data, scope.jurisdiction.id)
            _record_content_change(scope, "updated hearing type", item_id)
            return items[i]
    raise HTTPException(status_code=404, detail=f"Hearing type '{item_id}' not found")


@router.post("/document-content/hearing-types", status_code=201)
async def create_hearing_type(
    payload: HearingTypePayload,
    scope: LegalWordingScope = Depends(_LEGAL_WORDING_WRITE),
) -> dict[str, Any]:
    """Create a new hearing type."""
    data = _read_content(scope.jurisdiction.id)
    items: list[dict] = data.setdefault("hearing_types", [])
    if any(item.get("id") == payload.id for item in items):
        raise HTTPException(status_code=409, detail=f"Hearing type with id '{payload.id}' already exists")
    new_item = payload.model_dump()
    items.append(new_item)
    _write_content(data, scope.jurisdiction.id)
    _record_content_change(scope, "created hearing type", payload.id)
    return new_item


@router.delete("/document-content/hearing-types/{item_id}", status_code=204)
async def delete_hearing_type(
    item_id: str,
    scope: LegalWordingScope = Depends(_LEGAL_WORDING_WRITE),
) -> None:
    """Delete a hearing type by ID."""
    data = _read_content(scope.jurisdiction.id)
    items: list[dict] = data.get("hearing_types", [])
    original_len = len(items)
    data["hearing_types"] = [item for item in items if item.get("id") != item_id]
    if len(data["hearing_types"]) == original_len:
        raise HTTPException(status_code=404, detail=f"Hearing type '{item_id}' not found")
    _write_content(data, scope.jurisdiction.id)
    _record_content_change(scope, "deleted hearing type", item_id)


# ---------------------------------------------------------------------------
# Legal frameworks
# ---------------------------------------------------------------------------


@router.put("/document-content/legal-frameworks/{item_id}")
async def update_legal_framework(
    item_id: str,
    payload: LegalFrameworkPayload,
    scope: LegalWordingScope = Depends(_LEGAL_WORDING_WRITE),
) -> dict[str, Any]:
    """Update an existing legal framework by ID."""
    data = _read_content(scope.jurisdiction.id)
    items: list[dict] = data.get("legal_frameworks", [])
    for i, item in enumerate(items):
        if item.get("id") == item_id:
            items[i] = payload.model_dump()
            _write_content(data, scope.jurisdiction.id)
            _record_content_change(scope, "updated legal framework", item_id)
            return items[i]
    raise HTTPException(status_code=404, detail=f"Legal framework '{item_id}' not found")


@router.post("/document-content/legal-frameworks", status_code=201)
async def create_legal_framework(
    payload: LegalFrameworkPayload,
    scope: LegalWordingScope = Depends(_LEGAL_WORDING_WRITE),
) -> dict[str, Any]:
    """Create a new legal framework. A jurisdiction's first one turns its legal wording
    on: its form gets the legal issues step and its documents the Issues and Legal
    Framework sections."""
    data = _read_content(scope.jurisdiction.id)
    items: list[dict] = data.setdefault("legal_frameworks", [])
    if any(item.get("id") == payload.id for item in items):
        raise HTTPException(status_code=409, detail=f"Legal framework with id '{payload.id}' already exists")
    new_item = payload.model_dump()
    items.append(new_item)
    items.sort(key=lambda x: x.get("rank", 999))
    data["legal_frameworks"] = items
    _write_content(data, scope.jurisdiction.id)
    _record_content_change(scope, "created legal framework", payload.id)
    return new_item


@router.delete("/document-content/legal-frameworks/{item_id}", status_code=204)
async def delete_legal_framework(
    item_id: str,
    scope: LegalWordingScope = Depends(_LEGAL_WORDING_WRITE),
) -> None:
    """Delete a legal framework by ID. Deleting the last one turns the jurisdiction's
    legal wording off."""
    data = _read_content(scope.jurisdiction.id)
    items: list[dict] = data.get("legal_frameworks", [])
    original_len = len(items)
    data["legal_frameworks"] = [item for item in items if item.get("id") != item_id]
    if len(data["legal_frameworks"]) == original_len:
        raise HTTPException(status_code=404, detail=f"Legal framework '{item_id}' not found")
    _write_content(data, scope.jurisdiction.id)
    _record_content_change(scope, "deleted legal framework", item_id)


# ---------------------------------------------------------------------------
# Anonymity statuses
# ---------------------------------------------------------------------------


@router.put("/document-content/anonymity-statuses/{item_id}")
async def update_anonymity_status(
    item_id: str,
    payload: AnonymityStatusPayload,
    scope: LegalWordingScope = Depends(_LEGAL_WORDING_WRITE),
) -> dict[str, Any]:
    """Update an existing anonymity status by ID."""
    data = _read_content(scope.jurisdiction.id)
    statuses: list[dict] = data.get("anonymity", {}).get("statuses", [])
    for i, item in enumerate(statuses):
        if item.get("id") == item_id:
            statuses[i] = payload.model_dump()
            data["anonymity"]["statuses"] = statuses
            _write_content(data, scope.jurisdiction.id)
            _record_content_change(scope, "updated anonymity status", item_id)
            return statuses[i]
    raise HTTPException(status_code=404, detail=f"Anonymity status '{item_id}' not found")


# ---------------------------------------------------------------------------
# Admin transcript access
# ---------------------------------------------------------------------------


def _log_admin_transcript_access(
    *,
    admin_id: str,
    action: str,
    target_transcription_id: str | None = None,
    target_owner_id: str | None = None,
) -> None:
    logger.info(
        "ADMIN_TRANSCRIPT_ACCESS action=%s admin_id=%s "
        "target_transcription_id=%s target_owner_id=%s",
        action,
        sanitize_for_log(admin_id),
        sanitize_for_log(target_transcription_id) if target_transcription_id else "N/A",
        sanitize_for_log(target_owner_id) if target_owner_id else "N/A",
    )


@router.get("/transcriptions", response_model=AdminTranscriptionListResponse)
async def admin_list_transcriptions(
    owner_email: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> AdminTranscriptionListResponse:
    """List all transcriptions across all judges in the admin's jurisdiction (DIAAT-334),
    with optional filters. SystemAdministrator only."""
    if page < 1 or page_size < 1:
        raise HTTPException(status_code=422, detail="page and page_size must be >= 1")
    if page_size > 200:
        raise HTTPException(status_code=422, detail="page_size must be <= 200")

    transcriptions, total = admin_fetch_all_transcriptions(
        jurisdiction_id=jurisdiction.id,
        owner_email=owner_email,
        created_after=created_after,
        created_before=created_before,
        page=page,
        page_size=page_size,
    )
    _log_admin_transcript_access(
        admin_id=str(admin_user.id),
        action="LIST_TRANSCRIPTIONS",
    )
    def _to_utc(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    items = [
        AdminTranscriptionRecord(
            id=t.id,
            title=t.title,
            created_datetime=_to_utc(t.created_datetime),
            owner_id=t.user_id,
            owner_email=t.user.email if t.user else "unknown",
        )
        for t in transcriptions
    ]
    return AdminTranscriptionListResponse(items=items, total=total, page=page, page_size=page_size)


class AdminJobProvenance(BaseModel):
    """What produced one job's output (DIAAT-286, transcribe_api.domain.provenance_dictation.Provenance)."""

    job_id: UUID
    provenance: dict[str, Any] | None


class AdminTranscriptionDetail(BaseTable):
    """The transcription as this route returned it before DIAAT-286, plus the
    provenance of its outputs (additive).

    It was ``response_model=Transcription``: the model's fields (id, created_datetime,
    updated_datetime, user_id, jurisdiction_id since DIAAT-322, title), never its
    relationships. Declared as the model declares them, so names, types, order and
    the OpenAPI schema are unchanged.
    """

    user_id: UUID = Field(default=None)
    jurisdiction_id: UUID | None = Field(default=None)
    title: str | None = Field(default=None)
    provenance: list[AdminJobProvenance] = Field(default_factory=list)


@router.get("/transcriptions/{transcription_id}", response_model=AdminTranscriptionDetail)
async def admin_get_transcription(
    transcription_id: UUID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> AdminTranscriptionDetail:
    """View any transcription regardless of owner, with the provenance of its outputs.
    SystemAdministrator only. Audited (DIAAT-286) as a privileged view."""
    async with audited(
        TRANSCRIPT_VIEWED,
        actor_id=admin_user.id,
        object_type="transcription",
        object_id=transcription_id,
        detail={"privileged": True},
    ) as audit:
        transcription = admin_get_transcription_by_id(transcription_id)
        _admin_refuse_other_jurisdiction(transcription, admin_user, jurisdiction, TRANSCRIPTION, READ)
        audit.authorised()
    _log_admin_transcript_access(
        admin_id=str(admin_user.id),
        action="VIEW_TRANSCRIPTION",
        target_transcription_id=str(transcription_id),
        target_owner_id=str(transcription.user_id),
    )
    jobs = sorted(transcription.transcription_jobs or [], key=lambda job: job.created_datetime)
    return AdminTranscriptionDetail(
        id=transcription.id,
        user_id=transcription.user_id,
        jurisdiction_id=transcription.jurisdiction_id,
        title=transcription.title,
        created_datetime=transcription.created_datetime,
        updated_datetime=transcription.updated_datetime,
        provenance=[AdminJobProvenance(job_id=job.id, provenance=job.provenance) for job in jobs],
    )


@router.get("/transcriptions/{transcription_id}/document-download")
async def admin_get_document_download_url(
    transcription_id: UUID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> dict[str, str]:
    """Download any transcript document regardless of owner. SystemAdministrator only.
    Audited (DIAAT-286) as a privileged export."""
    async with audited(
        TRANSCRIPT_EXPORTED,
        actor_id=admin_user.id,
        object_type="document",
        object_id=transcription_id,
        detail={"via": "link", "privileged": True},
    ) as audit:
        return await _admin_document_download_url(transcription_id, admin_user, jurisdiction, audit)


async def _admin_document_download_url(
    transcription_id: UUID, admin_user: User, jurisdiction: ActiveJurisdiction, audit: AuditedAction
) -> dict[str, str]:
    transcription = admin_get_transcription_by_id(transcription_id)
    _admin_refuse_other_jurisdiction(transcription, admin_user, jurisdiction, DOCUMENT, READ)
    audit.authorised()

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

    title = transcription.title or "hearing-document"
    hearing_date = transcription.created_datetime.strftime("%Y-%m-%d") if transcription.created_datetime else ""
    ext = Path(blob_path).suffix or ".docx"
    download_filename = f"[{hearing_date}] {title}{ext}" if hearing_date else f"{title}{ext}"

    settings = get_settings()
    download_url = await generate_blob_download_url(
        container_name=settings.AZURE_STORAGE_CONTAINER_NAME,
        blob_name=blob_path,
        expiry_hours=settings.DOWNLOAD_URL_TTL_MINUTES / 60,
        download_filename=download_filename,
    )

    _log_admin_transcript_access(
        admin_id=str(admin_user.id),
        action="DOWNLOAD_DOCUMENT",
        target_transcription_id=str(transcription_id),
        target_owner_id=str(transcription.user_id),
    )

    return {"download_url": download_url}


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------


class AdminUserListItem(BaseModel):
    id: str
    email: str
    role: str
    has_completed_onboarding: bool
    created_datetime: datetime


class AdminUserListResponse(BaseModel):
    items: list[AdminUserListItem]
    total: int
    page: int
    page_size: int


class AdminUpdateUserRoleRequest(BaseModel):
    role: str


def _validate_role(role: str) -> str:
    valid = get_valid_roles()
    if role not in valid.values():
        raise HTTPException(
            status_code=422,
            detail=f"Invalid role '{role}'. Valid roles: {', '.join(valid.values())}",
        )
    return role


@router.get("/users", response_model=AdminUserListResponse)
async def admin_list_users_route(
    search_email: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> AdminUserListResponse:
    """List all registered users. SystemAdministrator only."""
    users, total = admin_list_users(page=page, page_size=page_size, search_email=search_email)
    logger.info(
        "ADMIN_USER_LIST admin_id=%s total=%d",
        sanitize_for_log(str(admin_user.id)),
        total,
    )
    items = [
        AdminUserListItem(
            id=str(u.id),
            email=u.email,
            role=u.role,
            has_completed_onboarding=u.has_completed_onboarding,
            created_datetime=u.created_datetime,
        )
        for u in users
    ]
    return AdminUserListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/users/{user_id}", response_model=AdminUserListItem)
async def admin_get_user_route(
    user_id: UUID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> AdminUserListItem:
    """Get a single user by ID. SystemAdministrator only."""
    user = get_user_by_id(user_id)
    logger.info(
        "ADMIN_USER_VIEW admin_id=%s target_user_id=%s",
        sanitize_for_log(str(admin_user.id)),
        sanitize_for_log(str(user_id)),
    )
    return AdminUserListItem(
        id=str(user.id),
        email=user.email,
        role=user.role,
        has_completed_onboarding=user.has_completed_onboarding,
        created_datetime=user.created_datetime,
    )


@router.put("/users/{user_id}/role", response_model=AdminUserListItem)
async def admin_update_user_role_route(
    user_id: UUID,
    payload: AdminUpdateUserRoleRequest,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> AdminUserListItem:
    """Update a user's role. SystemAdministrator only."""
    if user_id == admin_user.id:
        raise HTTPException(status_code=409, detail="Cannot modify your own account.")
    _validate_role(payload.role)
    target = get_user_by_id(user_id)
    old_role = target.role
    user = update_user(user_id, role=payload.role)
    logger.info(
        "ADMIN_USER_ROLE_CHANGE admin_id=%s target_user_id=%s old_role=%s new_role=%s",
        sanitize_for_log(str(admin_user.id)),
        sanitize_for_log(str(user_id)),
        sanitize_for_log(old_role),
        sanitize_for_log(payload.role),
    )
    app_roles: list[str] = admin_user.app_roles
    # In a worker thread: the insert must not stall the event loop.
    await asyncio.to_thread(write_audit_event, AuditEvent(
        event_type=AuditEventType.ROLE_CHANGE,
        user_id=str(admin_user.id),
        email=admin_user.email,
        held_roles=app_roles,
        required_roles=[_require_role("SystemAdministrator")],
        resource=f"user:{user_id}",
        detail={"target_user_id": str(user_id), "target_email": user.email, "old_role": old_role, "new_role": payload.role},
    ))
    return AdminUserListItem(
        id=str(user.id),
        email=user.email,
        role=user.role,
        has_completed_onboarding=user.has_completed_onboarding,
        created_datetime=user.created_datetime,
    )


@router.delete("/users/{user_id}", status_code=204)
async def admin_delete_user_route(
    user_id: UUID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> None:
    """Delete a user. Fails with 409 if the user still owns transcriptions. SystemAdministrator only."""
    if user_id == admin_user.id:
        raise HTTPException(status_code=409, detail="Cannot delete your own account.")
    target = get_user_by_id(user_id)
    try:
        admin_delete_user(user_id)
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except UserHasTranscriptionsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    logger.info(
        "ADMIN_USER_DELETED admin_id=%s target_user_id=%s",
        sanitize_for_log(str(admin_user.id)),
        sanitize_for_log(str(user_id)),
    )
    app_roles: list[str] = admin_user.app_roles
    # In a worker thread: the insert must not stall the event loop.
    await asyncio.to_thread(write_audit_event, AuditEvent(
        event_type=USER_DELETED,
        user_id=str(admin_user.id),
        email=admin_user.email,
        held_roles=app_roles,
        required_roles=[_require_role("SystemAdministrator")],
        resource=f"user:{user_id}",
        detail={"target_user_id": str(user_id), "target_email": target.email},
    ))


# ---------------------------------------------------------------------------
# Jurisdictions and their members (DIAAT-356, SystemAdministrator only)
# ---------------------------------------------------------------------------
# What jurisdictions_admin does from a command line, from the admin area, so nobody
# runs anything on a cluster. Plain defs: the database work is synchronous, so
# FastAPI runs them in the threadpool. Each change is audited.

_SYSTEM_ADMINISTRATOR = get_allowlisted_user(
    required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event
)


class JurisdictionAdminRecord(BaseModel):
    code: str
    name: str
    display_name: str | None
    service: str | None
    # DIAAT-345: draft (its admins see it), test (its testers too) or live (everyone).
    stage: Literal["draft", "test", "live"]
    enabled: bool
    auto_enrol: bool
    settings: dict[str, Any]
    member_count: int
    # What stops it being switched on, in plain words; empty when it is ready.
    problems: list[str]
    # Sent back with a save, so a change made meanwhile is not undone (409).
    version: str


class JurisdictionWrite(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    settings: dict[str, Any]
    # Left out: unchanged (a new jurisdiction is enabled, without auto-enrol, a draft).
    enabled: bool | None = None
    auto_enrol: bool | None = None
    stage: Literal["draft", "test", "live"] | None = None
    # Left out: unchanged; blank: none (the name is shown).
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    service: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    # The version edited ("" when adding one); left out, no check (the command line).
    version: str | None = None


class JurisdictionMemberRecord(BaseModel):
    email: str
    role: str
    # In the jurisdiction's test group (DIAAT-345).
    tester: bool
    pending: bool


class JurisdictionMemberWrite(BaseModel):
    email: str = PydanticField(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    role: str = "judge"
    # Left out: unchanged (a new member is not a tester).
    tester: bool | None = None


class WordingKeysResponse(BaseModel):
    keys: dict[str, str]


JURISDICTION_NOT_READY = "jurisdiction_not_ready"
TEMPLATE_NOT_READY = "template_not_ready"
TEMPLATE_VALUES_INVALID = "template_values_invalid"


@contextmanager
def _admin_errors():
    """A jurisdictions_admin refusal is the caller's 400, an unknown jurisdiction 404, a
    change made meanwhile 409."""
    try:
        yield
    except jurisdiction_templates.TemplateProblemsError as exc:  # DIAAT-344: which box
        raise HTTPException(
            status_code=400,
            detail={"code": TEMPLATE_NOT_READY, "message": str(exc), "problems": exc.problems},
        ) from None
    except jurisdiction_templates.TemplateValuesError as exc:  # DIAAT-346: which value
        raise HTTPException(
            status_code=400,
            detail={"code": TEMPLATE_VALUES_INVALID, "message": str(exc), "problems": exc.problems},
        ) from None
    except jurisdictions_admin.NotReadyError as exc:  # DIAAT-345: what is missing
        raise HTTPException(
            status_code=400,
            detail={"code": JURISDICTION_NOT_READY, "message": str(exc), "problems": exc.problems},
        ) from None
    except jurisdictions_admin.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except jurisdictions_admin.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except jurisdictions_admin.AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _admin_session_run(operation):
    """Run a jurisdictions_admin operation in a session, its errors mapped (_admin_errors)."""
    with _admin_errors(), Session(get_engine()) as session:
        return operation(session)


def _audit_jurisdiction_change(action: str, admin_user: User, detail: dict[str, Any]) -> None:
    record_audit(action, actor_id=admin_user.id, outcome="success", object_type="jurisdiction", detail=detail)


def _member_target(email: str) -> dict[str, Any]:
    """Who a membership change was for, without contact details (domain/audit.py): their
    user IDs once they have signed in, else a hash of the email they were added by
    (to check a known address against)."""
    with Session(get_engine()) as session:
        user_ids = jurisdictions_admin.user_ids_for(session, email)
    if user_ids:
        return {"target_user_ids": user_ids}
    return {"target_email_sha256": hashlib.sha256(email.strip().lower().encode()).hexdigest()}


@router.get("/jurisdictions", response_model=list[JurisdictionAdminRecord])
def admin_list_jurisdictions(
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> list[JurisdictionAdminRecord]:
    """Every jurisdiction, with its settings and member count."""
    return [JurisdictionAdminRecord(**row) for row in _admin_session_run(jurisdictions_admin.jurisdictions_overview)]


@router.get("/jurisdictions/wording-keys", response_model=WordingKeysResponse)
def admin_jurisdiction_wording_keys(
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> WordingKeysResponse:
    """The screen text a jurisdiction can set, with what each is for."""
    return WordingKeysResponse(keys=WORDING_KEYS)


@router.put("/jurisdictions/{code}", response_model=JurisdictionAdminRecord)
def admin_put_jurisdiction(
    code: str,
    body: JurisdictionWrite,
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> JurisdictionAdminRecord:
    """Create or update a jurisdiction. Its settings are validated first (400); a
    stale ``version`` is refused (409)."""
    created = False

    def put(session: Session) -> dict:
        nonlocal created
        outcome = jurisdictions_admin.put_jurisdiction(
            session,
            code,
            body.name,
            body.settings,
            auto_enrol=body.auto_enrol,
            enabled=body.enabled,
            expected_version=body.version,
            stage=body.stage,
            display_name=body.display_name,
            service=body.service,
        )
        created = outcome.startswith("Created")
        return next(r for r in jurisdictions_admin.jurisdictions_overview(session) if r["code"] == code.strip().upper())

    record = _admin_session_run(put)
    settings = record["settings"]
    _audit_jurisdiction_change(
        "jurisdiction.edit",
        admin_user,
        {
            "code": record["code"],
            "created": created,
            "name": record["name"],
            "display_name": record["display_name"],
            "service": record["service"],
            "stage": record["stage"],
            "enabled": record["enabled"],
            "auto_enrol": record["auto_enrol"],
            "template": settings.get("template"),
            "features": settings.get("features"),
            "wording_keys": sorted(settings.get("wording", {})),
        },
    )
    return JurisdictionAdminRecord(**record)


@router.get("/jurisdictions/{code}/members", response_model=list[JurisdictionMemberRecord])
def admin_list_jurisdiction_members(
    code: str,
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> list[JurisdictionMemberRecord]:
    """A jurisdiction's members, including people added who have not signed in yet."""
    return [
        JurisdictionMemberRecord(**m) for m in _admin_session_run(lambda s: jurisdictions_admin.members_of(s, code))
    ]


@router.post("/jurisdictions/{code}/members", response_model=JurisdictionMemberRecord, status_code=201)
def admin_add_jurisdiction_member(
    code: str,
    body: JurisdictionMemberWrite,
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> JurisdictionMemberRecord:
    """Add someone by their sign-in email, or change their role or whether they are a
    tester. Someone who has not signed in yet joins when they first do (pending)."""
    email = body.email.strip().lower()

    def add(session: Session) -> dict:
        jurisdictions_admin.add_member(session, code, email, body.role, tester=body.tester)
        return next(m for m in jurisdictions_admin.members_of(session, code) if m["email"] == email)

    member = _admin_session_run(add)
    _audit_jurisdiction_change(
        "membership.add",
        admin_user,
        {
            "code": code.strip().upper(),
            **_member_target(email),
            "role": member["role"],
            "tester": member["tester"],
            "pending": member["pending"],
        },
    )
    return JurisdictionMemberRecord(**member)


@router.delete("/jurisdictions/{code}/members", status_code=204)
def admin_remove_jurisdiction_member(
    code: str,
    email: str = Query(min_length=3, max_length=320),
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> None:
    """Remove someone from a jurisdiction (by email, as listed)."""
    _admin_session_run(lambda s: jurisdictions_admin.remove_member(s, code, email))
    _audit_jurisdiction_change(
        "membership.remove", admin_user, {"code": code.strip().upper(), **_member_target(email)}
    )


# ---------------------------------------------------------------------------
# A jurisdiction's Word templates (DIAAT-344, SystemAdministrator only)
# ---------------------------------------------------------------------------
# Uploaded as the raw request body with Content-Type exactly application/octet-stream,
# like the browser's other uploads: Front Door's WAF blocks multipart bodies, and lets
# this one through by a custom Allow rule (azure-platform-terraform). The file's name
# comes in X-Upload-Filename (URL-encoded).

UPLOAD_CONTENT_TYPE = "application/octet-stream"


class TemplateAdminRecord(BaseModel):
    id: str
    name: str
    service: str | None
    file_name: str
    enabled: bool
    boxes: list[dict[str, Any]]
    fields: list[dict[str, Any]]
    # What stops it being saved or enabled; empty when it is complete.
    problems: list[str]
    version: str


class TemplatePreviewRequest(BaseModel):
    # Sample hearing details, by box tag, as a judge's submission sends them.
    values: dict[str, str | bool | list[str] | None] = PydanticField(default_factory=dict, max_length=500)
    version: str


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class TemplateWrite(BaseModel):
    name: str = PydanticField(min_length=1, max_length=200)
    service: str | None = PydanticField(default=None, max_length=200)
    fields: list[TemplateField] = PydanticField(max_length=500)
    enabled: bool = False
    version: str


def _audit_template_change(action: str, admin_user: User, code: str, row_id: str, detail: dict[str, Any]) -> None:
    record_audit(
        action,
        actor_id=admin_user.id,
        outcome="success",
        object_type="template",
        object_id=row_id,
        detail={"code": code.strip().upper(), **detail},
    )


async def _read_template_upload(request: Request) -> tuple[str, bytes]:
    """The uploaded file's name and bytes: 415 for another Content-Type, 413 when too
    large, 400 for a file that is not a .docx by name."""
    content_type = (request.headers.get("content-type") or "").strip().lower()
    if content_type != UPLOAD_CONTENT_TYPE:
        raise HTTPException(status_code=415, detail=f"Send the file as {UPLOAD_CONTENT_TYPE}.")
    file_name = urllib.parse.unquote(request.headers.get("x-upload-filename") or "").strip()
    if not file_name.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Upload a Word document (.docx).")
    too_large = HTTPException(status_code=413, detail=f"The file is larger than {MAX_TEMPLATE_BYTES // (1024 * 1024)} MB.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_TEMPLATE_BYTES:
        raise too_large
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_TEMPLATE_BYTES:
            raise too_large
    return file_name, bytes(data)


@router.get("/jurisdictions/{code}/templates", response_model=list[TemplateAdminRecord])
def admin_list_templates(
    code: str,
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> list[TemplateAdminRecord]:
    """A jurisdiction's templates, each with its boxes, fields and what is missing."""
    rows = _admin_session_run(
        lambda s: [jurisdiction_templates.template_record(r) for r in jurisdiction_templates.templates_of(s, code)]
    )
    return [TemplateAdminRecord(**row) for row in rows]


@router.post("/jurisdictions/{code}/templates", response_model=TemplateAdminRecord)
async def admin_upload_template(
    code: str,
    request: Request,
    response: Response,
    replace: UUID | None = Query(default=None),
    version: str | None = Query(default=None, max_length=64),
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> TemplateAdminRecord:
    """Upload a Word template: a new one (201), with a field proposed for each box, or
    with ``replace`` and its ``version`` a new file for an existing one, keeping the
    fields of the boxes it still has. A file BenchNotes cannot use is refused (400)."""
    file_name, data = await _read_template_upload(request)

    def upload(session: Session) -> dict:
        if replace is None:
            row = jurisdiction_templates.add_template(session, code, file_name, data)
        else:
            row = jurisdiction_templates.replace_file(session, code, replace, file_name, data, version or "")
        return jurisdiction_templates.template_record(row)

    record = await asyncio.to_thread(_admin_session_run, upload)
    if replace is None:
        response.status_code = 201
    _audit_template_change(
        "template.add" if replace is None else "template.edit",
        admin_user,
        code,
        record["id"],
        {"change": "uploaded" if replace is None else "file_replaced", "boxes": len(record["boxes"])},
    )
    return TemplateAdminRecord(**record)


@router.put("/jurisdictions/{code}/templates/{template_id}", response_model=TemplateAdminRecord)
def admin_update_template(
    code: str,
    template_id: UUID,
    body: TemplateWrite,
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> TemplateAdminRecord:
    """Save a template's name, fields and whether judges can choose it. Refused (400,
    ``template_not_ready``, with ``problems`` naming each box) unless every box has a
    complete field; 409 if it changed since ``version``."""
    record = _admin_session_run(
        lambda s: jurisdiction_templates.template_record(
            jurisdiction_templates.update_template(
                s,
                code,
                template_id,
                name=body.name,
                service=body.service,
                fields=body.fields,
                enabled=body.enabled,
                expected_version=body.version,
            )
        )
    )
    _audit_template_change(
        "template.edit", admin_user, code, record["id"], {"change": "fields", "enabled": record["enabled"]}
    )
    return TemplateAdminRecord(**record)


@router.get("/jurisdictions/{code}/templates/{template_id}/form", response_model=AvailableTemplate)
def admin_template_form(
    code: str,
    template_id: UUID,
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> AvailableTemplate:
    """DIAAT-346: the template's Hearing Details form as its judges will get it, for
    the admin to fill with sample details, whether or not judges can choose it yet."""

    row = _admin_session_run(lambda s: jurisdiction_templates.template_of(s, code, template_id))
    (result,) = template_forms([row], row.jurisdiction_id)
    return result


@router.post("/jurisdictions/{code}/templates/{template_id}/preview")
def admin_preview_template(
    code: str,
    template_id: UUID,
    body: TemplatePreviewRequest,
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> Response:
    """DIAAT-346: the template filled with sample hearing details, as a judge would get
    it (a .docx), before judges can choose it. Refused in plain words (400, with
    ``problems``): ``template_not_ready`` while a box has no complete field,
    ``template_values_invalid`` for details that do not fit; 409 if it changed since
    ``version``."""
    row, jurisdiction = _admin_session_run(
        lambda s: jurisdiction_templates.preview_source(s, code, template_id, body.version)
    )
    # Filled once the session is closed, as a judge's submission is.
    with _admin_errors():
        name, document = jurisdiction_templates.preview_template(row, jurisdiction, body.values)
    file_name = urllib.parse.quote(f"Preview - {name}.docx")
    return Response(
        content=document,
        media_type=DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{file_name}", "Cache-Control": "no-store"},
    )


@router.delete("/jurisdictions/{code}/templates/{template_id}", status_code=204)
def admin_delete_template(
    code: str,
    template_id: UUID,
    admin_user: User = Depends(_SYSTEM_ADMINISTRATOR),
) -> None:
    """Delete a template. Documents already made from it are kept."""
    _admin_session_run(lambda s: jurisdiction_templates.delete_template(s, code, template_id))
    _audit_template_change("template.delete", admin_user, code, str(template_id), {})
