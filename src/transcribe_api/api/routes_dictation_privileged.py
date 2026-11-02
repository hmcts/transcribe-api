"""Admin API routes for managing document content (legal texts).

All endpoints require authenticated users to pass JWT role checks.
"""

from __future__ import annotations

import copy
import logging
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from transcribe_api.api.document_content_models import (
    AdminJurisdictionSummary,
    AnonymityStatusPayload,
    CreateJurisdictionPayload,
    DocumentContentResponse,
    HearingTypePayload,
    JurisdictionAdminItem,
    JurisdictionRef,
    JurisdictionSummary,
    LegalFrameworkPayload,
)
from transcribe_api.stt.utils import generate_blob_download_url
from transcribe_api.domain.audit import write_audit_event
from transcribe_api.domain.exceptions import UserHasTranscriptionsError, UserNotFoundError
from hmcts_azure_auth.audit import AuditEvent, AuditEventType
from transcribe_api.domain.interface_dictation import (
    admin_delete_user,
    admin_fetch_all_transcriptions,
    admin_get_transcription_by_id,
    admin_list_users,
    get_user_by_id,
    update_user,
)
from transcribe_api.domain.auth.approles_dictation import get_valid_roles
from transcribe_api.domain.jurisdictions import (
    JurisdictionExistsError,
    add_jurisdiction_admin,
    create_jurisdiction,
    get_admin_jurisdiction_ids,
    get_jurisdiction,
    list_jurisdiction_admins,
    list_jurisdictions,
    remove_jurisdiction_admin,
)
from transcribe_api.domain.models_dictation import Jurisdiction, Transcription
from transcribe_api.documents.content_loader import get_document_content as load_document_content
from transcribe_api.documents.content_loader import has_legal_wording, save_document_content
from transcribe_api.documents.models import DEFAULT_JURISDICTION_ID
from transcribe_api.documents.minutes.types import AdminTranscriptionListResponse, AdminTranscriptionRecord
from transcribe_api.domain.auth.approles_dictation import get_role
from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
from transcribe_api.runtime.log_sanitization import sanitize_for_log
from transcribe_api.runtime.settings_dictation import get_settings

if TYPE_CHECKING:
    from transcribe_api.domain.models_dictation import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminUserResponse(BaseModel):
    email: str
    role: str = "admin"
    user_id: str
    is_admin: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_content(jurisdiction_id: str = DEFAULT_JURISDICTION_ID) -> dict[str, Any]:
    try:
        # A copy: the loader's cached dict must not be edited in place before a save succeeds.
        return copy.deepcopy(load_document_content(jurisdiction_id))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unable to load document content") from exc


def _write_content(data: dict[str, Any], jurisdiction_id: str = DEFAULT_JURISDICTION_ID) -> None:
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
    admin_user: User = Depends(get_allowlisted_user(required_roles_any=[_require_role("LegalTextManager"), _require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008, ARG001
) -> list[str]:
    return list(get_valid_roles().values())


# ---------------------------------------------------------------------------
# Jurisdiction scope (DIAAT-342)
#
# Wording is kept per jurisdiction. A LegalTextManager may read and edit only the
# jurisdictions mapped to them in jurisdiction_admin. A SystemAdministrator may read
# every jurisdiction, and edit every one if they also hold LegalTextManager (they
# manage the mapping, so could grant themselves the access anyway). Every wording
# route calls _authorise_jurisdiction before it reads or writes anything.
# ---------------------------------------------------------------------------


def _authorise_jurisdiction(admin_user: User, jurisdiction_id: str, *, write: bool) -> Jurisdiction:
    """Return the jurisdiction if this admin may read (or, with write, edit) its wording.

    Raises 404 both for a jurisdiction that does not exist and for one the admin is
    not allowed, so the response never reveals which jurisdictions exist or what they hold.
    Denials for an existing jurisdiction are audited.
    """
    jurisdiction = get_jurisdiction(jurisdiction_id)
    if jurisdiction is None:
        raise HTTPException(status_code=404, detail="Jurisdiction not found")

    app_roles: list[str] = admin_user.app_roles
    system_admin = _require_role("SystemAdministrator") in app_roles
    can_edit_everything = system_admin and _require_role("LegalTextManager") in app_roles
    if (system_admin and not write) or can_edit_everything:
        return jurisdiction
    if jurisdiction_id in get_admin_jurisdiction_ids(admin_user.id):
        return jurisdiction

    write_audit_event(AuditEvent(
        event_type=AuditEventType.ACCESS_DENIED,
        user_id=str(admin_user.id),
        email=admin_user.email,
        held_roles=app_roles,
        required_roles=[_require_role("LegalTextManager")],
        resource=f"jurisdiction:{jurisdiction_id}",
        detail={"jurisdiction_id": jurisdiction_id, "write": write},
    ))
    raise HTTPException(status_code=404, detail="Jurisdiction not found")


def _content_response(jurisdiction: Jurisdiction, data: dict[str, Any]) -> DocumentContentResponse:
    return DocumentContentResponse(
        jurisdiction=JurisdictionRef(id=jurisdiction.id, name=jurisdiction.name),
        has_legal_wording=bool(data.get("legal_frameworks")),
        hearing_types=data.get("hearing_types", []),
        legal_frameworks=data.get("legal_frameworks", []),
        anonymity=data.get("anonymity", {}),
    )


def _scoped(method: str, suffix: str = "", **route_kwargs: Any):
    """Register a wording handler under /admin/jurisdictions/{jurisdiction_id}/document-content{suffix}.

    The same handler is also registered, deprecated, under the pre-DIAAT-342
    /admin/document-content{suffix} path, where jurisdiction_id is an optional query
    parameter defaulting to IAC. Old web builds that only know that path keep working
    for IAC admins, and are authorised exactly like the new routes: an admin not mapped
    to IAC gets 404 from them.
    """

    def decorator(func):
        register = getattr(router, method)
        register(f"/jurisdictions/{{jurisdiction_id}}/document-content{suffix}", **route_kwargs)(func)
        register(f"/document-content{suffix}", deprecated=True, **route_kwargs)(func)
        return func

    return decorator


# ---------------------------------------------------------------------------
# Jurisdictions and their admins
# ---------------------------------------------------------------------------


@router.get("/jurisdictions", response_model=list[AdminJurisdictionSummary])
async def list_admin_jurisdictions(
    admin_user: User = Depends(get_allowlisted_user(required_roles_any=[_require_role("LegalTextManager"), _require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> list[AdminJurisdictionSummary]:
    """Return the jurisdictions whose wording this admin may open.

    A LegalTextManager gets the jurisdictions they are mapped to (can_edit true). A
    SystemAdministrator gets all of them, with can_edit true only if they also hold
    LegalTextManager.
    """
    app_roles: list[str] = admin_user.app_roles
    if _require_role("SystemAdministrator") in app_roles:
        visible = None
        can_edit = _require_role("LegalTextManager") in app_roles
    else:
        visible = get_admin_jurisdiction_ids(admin_user.id)
        can_edit = True
    return [
        AdminJurisdictionSummary(
            id=j.id, name=j.name, has_legal_wording=has_legal_wording(j.id), can_edit=can_edit
        )
        for j in list_jurisdictions()
        if visible is None or j.id in visible
    ]


@router.post("/jurisdictions", status_code=201, response_model=JurisdictionSummary)
async def create_jurisdiction_route(
    payload: CreateJurisdictionPayload,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> JurisdictionSummary:
    """Create a jurisdiction. SystemAdministrator only.

    It starts with the shipped hearing types and anonymity wording and no legal
    frameworks. Map admins to it with PUT /admin/jurisdictions/{id}/admins/{user_id}.
    """
    taken = {j.id.lower() for j in list_jurisdictions()} | {j.name.lower() for j in list_jurisdictions()}
    if payload.id in taken or payload.name.lower() in taken:
        raise HTTPException(status_code=409, detail="A jurisdiction with this id or name already exists")
    try:
        jurisdiction = create_jurisdiction(payload.id, payload.name)
    except JurisdictionExistsError:
        raise HTTPException(status_code=409, detail="A jurisdiction with this id or name already exists") from None
    _log_admin_content_change(admin_id=str(admin_user.id), action="created jurisdiction", item_id=payload.id)
    return JurisdictionSummary(id=jurisdiction.id, name=jurisdiction.name, has_legal_wording=False)


def _admin_item(user: User) -> JurisdictionAdminItem:
    return JurisdictionAdminItem(user_id=str(user.id), email=user.email, role=user.role)


@router.get("/jurisdictions/{jurisdiction_id}/admins", response_model=list[JurisdictionAdminItem])
async def list_jurisdiction_admins_route(
    jurisdiction_id: str,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008, ARG001
) -> list[JurisdictionAdminItem]:
    """List the users mapped to a jurisdiction as its admins. SystemAdministrator only."""
    if get_jurisdiction(jurisdiction_id) is None:
        raise HTTPException(status_code=404, detail="Jurisdiction not found")
    return [_admin_item(u) for u in list_jurisdiction_admins(jurisdiction_id)]


@router.put("/jurisdictions/{jurisdiction_id}/admins/{user_id}", response_model=JurisdictionAdminItem)
async def add_jurisdiction_admin_route(
    jurisdiction_id: str,
    user_id: UUID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> JurisdictionAdminItem:
    """Map a user to a jurisdiction (idempotent). SystemAdministrator only.

    The user still needs the LegalTextManager role (from Entra) to reach the admin
    routes at all; this mapping says which jurisdictions that role applies to.
    """
    if get_jurisdiction(jurisdiction_id) is None:
        raise HTTPException(status_code=404, detail="Jurisdiction not found")
    target = get_user_by_id(user_id)
    if add_jurisdiction_admin(jurisdiction_id, user_id):
        _log_jurisdiction_admin_change(admin_user, "JURISDICTION_ADMIN_ADDED", jurisdiction_id, target)
    return _admin_item(target)


@router.delete("/jurisdictions/{jurisdiction_id}/admins/{user_id}", status_code=204)
async def remove_jurisdiction_admin_route(
    jurisdiction_id: str,
    user_id: UUID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> None:
    """Unmap a user from a jurisdiction. SystemAdministrator only. 404 if they were not mapped."""
    if get_jurisdiction(jurisdiction_id) is None:
        raise HTTPException(status_code=404, detail="Jurisdiction not found")
    target = get_user_by_id(user_id)
    if not remove_jurisdiction_admin(jurisdiction_id, user_id):
        raise HTTPException(status_code=404, detail="User is not an admin of this jurisdiction")
    _log_jurisdiction_admin_change(admin_user, "JURISDICTION_ADMIN_REMOVED", jurisdiction_id, target)


def _log_jurisdiction_admin_change(admin_user: User, event_type: str, jurisdiction_id: str, target: User) -> None:
    logger.info(
        "%s admin_id=%s target_user_id=%s jurisdiction_id=%s",
        event_type,
        sanitize_for_log(str(admin_user.id)),
        sanitize_for_log(str(target.id)),
        sanitize_for_log(jurisdiction_id),
    )
    write_audit_event(AuditEvent(
        event_type=event_type,
        user_id=str(admin_user.id),
        email=admin_user.email,
        held_roles=admin_user.app_roles,
        required_roles=[_require_role("SystemAdministrator")],
        resource=f"jurisdiction:{jurisdiction_id}",
        detail={"jurisdiction_id": jurisdiction_id, "target_user_id": str(target.id), "target_email": target.email},
    ))


# ---------------------------------------------------------------------------
# Read all
# ---------------------------------------------------------------------------


@_scoped("get", response_model=DocumentContentResponse)
async def get_document_content(
    jurisdiction_id: str = DEFAULT_JURISDICTION_ID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_any=[_require_role("LegalTextManager"), _require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> DocumentContentResponse:
    """Return one jurisdiction's full document_content payload."""
    jurisdiction = _authorise_jurisdiction(admin_user, jurisdiction_id, write=False)
    return _content_response(jurisdiction, _read_content(jurisdiction_id))


# ---------------------------------------------------------------------------
# Hearing types
# ---------------------------------------------------------------------------


@_scoped("put", "/hearing-types/{item_id}")
async def update_hearing_type(
    item_id: str,
    payload: HearingTypePayload,
    jurisdiction_id: str = DEFAULT_JURISDICTION_ID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("LegalTextManager")], audit_writer=write_audit_event)),  # noqa: B008
) -> dict[str, Any]:
    """Update an existing hearing type by ID."""
    _authorise_jurisdiction(admin_user, jurisdiction_id, write=True)
    data = _read_content(jurisdiction_id)
    items: list[dict] = data.get("hearing_types", [])
    for i, item in enumerate(items):
        if item.get("id") == item_id:
            items[i] = payload.model_dump()
            _write_content(data, jurisdiction_id)
            _log_admin_content_change(
                admin_id=str(admin_user.id),
                action="updated hearing type",
                item_id=item_id,
            )
            return items[i]
    raise HTTPException(status_code=404, detail=f"Hearing type '{item_id}' not found")


@_scoped("post", "/hearing-types", status_code=201)
async def create_hearing_type(
    payload: HearingTypePayload,
    jurisdiction_id: str = DEFAULT_JURISDICTION_ID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("LegalTextManager")], audit_writer=write_audit_event)),  # noqa: B008
) -> dict[str, Any]:
    """Create a new hearing type."""
    _authorise_jurisdiction(admin_user, jurisdiction_id, write=True)
    data = _read_content(jurisdiction_id)
    items: list[dict] = data.setdefault("hearing_types", [])
    if any(item.get("id") == payload.id for item in items):
        raise HTTPException(status_code=409, detail=f"Hearing type with id '{payload.id}' already exists")
    new_item = payload.model_dump()
    items.append(new_item)
    _write_content(data, jurisdiction_id)
    _log_admin_content_change(
        admin_id=str(admin_user.id),
        action="created hearing type",
        item_id=payload.id,
    )
    return new_item


@_scoped("delete", "/hearing-types/{item_id}", status_code=204)
async def delete_hearing_type(
    item_id: str,
    jurisdiction_id: str = DEFAULT_JURISDICTION_ID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("LegalTextManager")], audit_writer=write_audit_event)),  # noqa: B008
) -> None:
    """Delete a hearing type by ID."""
    _authorise_jurisdiction(admin_user, jurisdiction_id, write=True)
    data = _read_content(jurisdiction_id)
    items: list[dict] = data.get("hearing_types", [])
    original_len = len(items)
    data["hearing_types"] = [item for item in items if item.get("id") != item_id]
    if len(data["hearing_types"]) == original_len:
        raise HTTPException(status_code=404, detail=f"Hearing type '{item_id}' not found")
    _write_content(data, jurisdiction_id)
    _log_admin_content_change(
        admin_id=str(admin_user.id),
        action="deleted hearing type",
        item_id=item_id,
    )


# ---------------------------------------------------------------------------
# Legal frameworks
# ---------------------------------------------------------------------------


@_scoped("put", "/legal-frameworks/{item_id}")
async def update_legal_framework(
    item_id: str,
    payload: LegalFrameworkPayload,
    jurisdiction_id: str = DEFAULT_JURISDICTION_ID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("LegalTextManager")], audit_writer=write_audit_event)),  # noqa: B008
) -> dict[str, Any]:
    """Update an existing legal framework by ID."""
    _authorise_jurisdiction(admin_user, jurisdiction_id, write=True)
    data = _read_content(jurisdiction_id)
    items: list[dict] = data.get("legal_frameworks", [])
    for i, item in enumerate(items):
        if item.get("id") == item_id:
            items[i] = payload.model_dump()
            _write_content(data, jurisdiction_id)
            _log_admin_content_change(
                admin_id=str(admin_user.id),
                action="updated legal framework",
                item_id=item_id,
            )
            return items[i]
    raise HTTPException(status_code=404, detail=f"Legal framework '{item_id}' not found")


@_scoped("post", "/legal-frameworks", status_code=201)
async def create_legal_framework(
    payload: LegalFrameworkPayload,
    jurisdiction_id: str = DEFAULT_JURISDICTION_ID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("LegalTextManager")], audit_writer=write_audit_event)),  # noqa: B008
) -> dict[str, Any]:
    """Create a new legal framework.

    The first one gives a jurisdiction legal wording: its judges' form then has
    the legal issues step and its documents the Issues and Legal Framework sections.
    """
    _authorise_jurisdiction(admin_user, jurisdiction_id, write=True)
    data = _read_content(jurisdiction_id)
    items: list[dict] = data.setdefault("legal_frameworks", [])
    if any(item.get("id") == payload.id for item in items):
        raise HTTPException(status_code=409, detail=f"Legal framework with id '{payload.id}' already exists")
    new_item = payload.model_dump()
    items.append(new_item)
    items.sort(key=lambda x: x.get("rank", 999))
    data["legal_frameworks"] = items
    _write_content(data, jurisdiction_id)
    _log_admin_content_change(
        admin_id=str(admin_user.id),
        action="created legal framework",
        item_id=payload.id,
    )
    return new_item


@_scoped("delete", "/legal-frameworks/{item_id}", status_code=204)
async def delete_legal_framework(
    item_id: str,
    jurisdiction_id: str = DEFAULT_JURISDICTION_ID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("LegalTextManager")], audit_writer=write_audit_event)),  # noqa: B008
) -> None:
    """Delete a legal framework by ID. Deleting the last one leaves the jurisdiction with no legal wording."""
    _authorise_jurisdiction(admin_user, jurisdiction_id, write=True)
    data = _read_content(jurisdiction_id)
    items: list[dict] = data.get("legal_frameworks", [])
    original_len = len(items)
    data["legal_frameworks"] = [item for item in items if item.get("id") != item_id]
    if len(data["legal_frameworks"]) == original_len:
        raise HTTPException(status_code=404, detail=f"Legal framework '{item_id}' not found")
    _write_content(data, jurisdiction_id)
    _log_admin_content_change(
        admin_id=str(admin_user.id),
        action="deleted legal framework",
        item_id=item_id,
    )


# ---------------------------------------------------------------------------
# Anonymity statuses
# ---------------------------------------------------------------------------


@_scoped("put", "/anonymity-statuses/{item_id}")
async def update_anonymity_status(
    item_id: str,
    payload: AnonymityStatusPayload,
    jurisdiction_id: str = DEFAULT_JURISDICTION_ID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("LegalTextManager")], audit_writer=write_audit_event)),  # noqa: B008
) -> dict[str, Any]:
    """Update an existing anonymity status by ID."""
    _authorise_jurisdiction(admin_user, jurisdiction_id, write=True)
    data = _read_content(jurisdiction_id)
    statuses: list[dict] = data.get("anonymity", {}).get("statuses", [])
    for i, item in enumerate(statuses):
        if item.get("id") == item_id:
            statuses[i] = payload.model_dump()
            data["anonymity"]["statuses"] = statuses
            _write_content(data, jurisdiction_id)
            _log_admin_content_change(
                admin_id=str(admin_user.id),
                action="updated anonymity status",
                item_id=item_id,
            )
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
) -> AdminTranscriptionListResponse:
    """List all transcriptions across all judges, with optional filters. SystemAdministrator only."""
    if page < 1 or page_size < 1:
        raise HTTPException(status_code=422, detail="page and page_size must be >= 1")
    if page_size > 200:
        raise HTTPException(status_code=422, detail="page_size must be <= 200")

    transcriptions, total = admin_fetch_all_transcriptions(
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


@router.get("/transcriptions/{transcription_id}", response_model=Transcription)
async def admin_get_transcription(
    transcription_id: UUID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> Transcription:
    """View any transcription regardless of owner. SystemAdministrator only."""
    transcription = admin_get_transcription_by_id(transcription_id)
    _log_admin_transcript_access(
        admin_id=str(admin_user.id),
        action="VIEW_TRANSCRIPTION",
        target_transcription_id=str(transcription_id),
        target_owner_id=str(transcription.user_id),
    )
    return transcription


@router.get("/transcriptions/{transcription_id}/document-download")
async def admin_get_document_download_url(
    transcription_id: UUID,
    admin_user: User = Depends(get_allowlisted_user(required_roles_all=[_require_role("SystemAdministrator")], audit_writer=write_audit_event)),  # noqa: B008
) -> dict[str, str]:
    """Download any transcript document regardless of owner. SystemAdministrator only."""
    transcription = admin_get_transcription_by_id(transcription_id)

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
    write_audit_event(AuditEvent(
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
    write_audit_event(AuditEvent(
        event_type="USER_DELETED",
        user_id=str(admin_user.id),
        email=admin_user.email,
        held_roles=app_roles,
        required_roles=[_require_role("SystemAdministrator")],
        resource=f"user:{user_id}",
        detail={"target_user_id": str(user_id), "target_email": target.email},
    ))
