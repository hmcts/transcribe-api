"""Admin API routes: legal wording, the interim user and jurisdiction administration.

Every route declares the permission it needs (DIAAT-376, domain/auth/authorisation.py,
docs/authz-permissions.md). ``/admin/transcriptions*`` (any user's dictation records, for a
SystemAdministrator) and the user role and delete endpoints are gone (DIAAT-389, DIAAT-373);
the assignment-based replacements are Phase 4.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, StringConstraints
from pydantic import Field as PydanticField
from sqlmodel import Session, select

from transcribe_api import jurisdiction_templates, jurisdictions_admin
from transcribe_api.api.document_content_models import (
    AdminDocumentContentResponse,
    AnonymityStatusPayload,
    HearingTypePayload,
    JurisdictionRef,
    LegalFrameworkPayload,
)
from transcribe_api.documents.content_loader import get_document_content as load_document_content
from transcribe_api.documents.content_loader import has_legal_wording, save_document_content
from transcribe_api.documents.uploaded_templates import MAX_TEMPLATE_BYTES, TemplateField
from transcribe_api.domain.audit import record_audit
from transcribe_api.domain.auth import context as auth_context
from transcribe_api.domain.auth.authorisation import (
    assert_jurisdiction_in_scope,
    ensure_permission,
    has_permission,
    jurisdiction_scope,
)
from transcribe_api.domain.auth.dependencies_dictation import (
    get_active_jurisdiction,
    require_permission,
)
from transcribe_api.domain.auth.permissions import assignable_roles
from transcribe_api.domain.interface_dictation import (
    admin_list_users,
    get_user_by_id,
    user_in_jurisdictions,
)
from transcribe_api.domain.jurisdictions import (
    ROLE_ADMIN,
    TEMPLATE_IAC_DECISION,
    WORDING_KEYS,
    ActiveJurisdiction,
)
from transcribe_api.domain.models_dictation import Jurisdiction
from transcribe_api.runtime.db_connection import get_engine
from transcribe_api.runtime.log_sanitization import sanitize_for_log

if TYPE_CHECKING:
    from transcribe_api.domain.models_dictation import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminUserResponse(BaseModel):
    email: str
    # The stored role the caller acts as; null for a SystemAdministrator with no context.
    role: str | None
    user_id: str


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


# ---------------------------------------------------------------------------
# Admin identity
# ---------------------------------------------------------------------------


@router.get("/me", response_model=AdminUserResponse)
async def admin_me(
    admin_user: User = Depends(require_permission("admin.access")),  # noqa: B008
) -> AdminUserResponse:
    """The admin landing page's identity check: who the caller is and the role they are acting
    as. Needs ``admin.access`` (LegalTextManager, JurisdictionAdministrator,
    JurisdictionSuperAdministrator in their context; a SystemAdministrator for the system pages).
    ``role`` is the stored role of the context, never SystemAdministrator (invisible in the app:
    null for a SystemAdministrator acting in no context)."""
    ctx = auth_context.context_of(admin_user)
    return AdminUserResponse(
        email=admin_user.email,
        role=ctx.role if ctx else None,
        user_id=str(admin_user.id),
    )


class RolesResponse(BaseModel):
    roles: list[str]


@router.get("/roles", response_model=RolesResponse)
async def get_admin_roles(
    admin_user: User = Depends(
        require_permission("users.manage", "jurisdiction_admin.assign", "jurisdiction_super_admin.assign")
    ),
) -> RolesResponse:
    """The roles the caller may assign (DIAAT-374: ``{"roles": [...]}``, which is what the Users
    tab reads), by the contract's assignment rules for the role they act in: a
    JurisdictionAdministrator Judge and LegalTextManager; a JurisdictionSuperAdministrator also
    JurisdictionAdministrator; a SystemAdministrator JurisdictionAdministrator and
    JurisdictionSuperAdministrator. Never SystemAdministrator, which nobody can grant."""
    ctx = auth_context.context_of(admin_user)
    return RolesResponse(roles=assignable_roles(ctx.role if ctx else None, auth_context.sysadmin_of(admin_user)))


# ---------------------------------------------------------------------------
# Legal wording access (DIAAT-342, DIAAT-376)
#
# The routes below edit the legal wording (hearing types, legal frameworks, anonymity
# text) of ONE jurisdiction: the one the request works in (the token's context, as for every
# route). Who may: whoever holds ``legal_text.manage`` in it, which is the LegalTextManager
# acting in that jurisdiction (DIAAT-373; a JurisdictionAdministrator edits the screen wording
# of its jurisdiction, not its legal text, and a SystemAdministrator reads no jurisdiction's
# material). A jurisdiction without the IAC decision template has no legal wording to edit
# (404, as for its judges).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegalWordingScope:
    """Who is editing which jurisdiction's legal wording."""

    user: User
    jurisdiction: ActiveJurisdiction


async def _legal_wording_scope(
    response: Response,
    current_user: User = Depends(require_permission("legal_text.manage")),  # noqa: B008
    jurisdiction: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> LegalWordingScope:
    response.headers["Cache-Control"] = "no-store"
    if jurisdiction.settings.template != TEMPLATE_IAC_DECISION:
        raise HTTPException(status_code=404, detail="Not available in this jurisdiction")
    return LegalWordingScope(user=current_user, jurisdiction=jurisdiction)


_LEGAL_WORDING_READ = _legal_wording_scope
_LEGAL_WORDING_WRITE = _legal_wording_scope


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
# Admin user management
# ---------------------------------------------------------------------------


class AdminUserListItem(BaseModel):
    id: str
    email: str
    # Retired (DIAAT-382): a user's roles are their assignments; None for a user created
    # since. This list is replaced by the assignment-based one (DIAAT-383).
    role: str | None = None
    has_completed_onboarding: bool
    created_datetime: datetime


class AdminUserListResponse(BaseModel):
    items: list[AdminUserListItem]
    total: int
    page: int
    page_size: int


# Who may list users: those who manage a jurisdiction's users, and the SystemAdministrator (to find
# who to make a JurisdictionSuperAdministrator).
_USER_ADMIN = require_permission("users.manage", "jurisdiction_super_admin.assign")


def _user_scope(admin_user: User) -> frozenset[str] | None:
    """The jurisdictions whose users the caller sees: None for all (a global role, or a
    SystemAdministrator), else the one they are working in."""
    if has_permission(admin_user, "jurisdiction_super_admin.assign"):
        return None
    return jurisdiction_scope(admin_user)


@router.get("/users", response_model=AdminUserListResponse)
async def admin_list_users_route(
    search_email: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    admin_user: User = Depends(_USER_ADMIN),
) -> AdminUserListResponse:
    """List registered users (interim list, replaced by the assignment-based one in Phase 4):
    all of them for a JurisdictionSuperAdministrator or a SystemAdministrator, only those with
    a role in the jurisdiction they are working in for a JurisdictionAdministrator."""
    users, total = admin_list_users(
        page=page, page_size=page_size, search_email=search_email, jurisdiction_codes=_user_scope(admin_user)
    )
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
    admin_user: User = Depends(_USER_ADMIN),
) -> AdminUserListItem:
    """Get a single user by ID (the same people as the list: a user outside the caller's
    scope is the 404 of an unknown one)."""
    user = get_user_by_id(user_id)
    scope = _user_scope(admin_user)
    if scope is not None and not user_in_jurisdictions(user_id, scope):
        raise HTTPException(status_code=404, detail="User not found")
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


# ---------------------------------------------------------------------------
# Jurisdictions and their members (DIAAT-356, DIAAT-376)
# ---------------------------------------------------------------------------
# What jurisdictions_admin does from a command line, from the admin area, so nobody
# runs anything on a cluster. Plain defs: the database work is synchronous, so
# FastAPI runs them in the threadpool. Each change is audited.
#
# ``jurisdictions.manage``, a jurisdiction-scoped permission: a JurisdictionAdministrator
# sees and changes only the jurisdiction they are working in (403 ``jurisdiction_not_in_scope``
# for another), a JurisdictionSuperAdministrator any. Creating one also needs
# ``jurisdiction.create`` (JurisdictionSuperAdministrator). A SystemAdministrator has none of
# these (DIAAT-389). The members endpoints are interim: Phase 4 replaces them with the
# assignment endpoints.

_JURISDICTION_ADMIN = require_permission("jurisdictions.manage")


class JurisdictionAdminRecord(BaseModel):
    code: str
    name: str
    display_name: str | None
    service: str | None
    # DIAAT-345: draft (its admins see it), test (its testers too) or live (everyone).
    stage: Literal["draft", "test", "live"]
    enabled: bool
    settings: dict[str, Any]
    member_count: int
    # What stops it being switched on, in plain words; empty when it is ready.
    problems: list[str]
    # Sent back with a save, so a change made meanwhile is not undone (409).
    version: str


class JurisdictionWrite(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    settings: dict[str, Any]
    # Left out: unchanged (a new jurisdiction is enabled, a draft).
    enabled: bool | None = None
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


def _admin_session_run(operation):
    """Run a jurisdictions_admin operation; its refusals are the caller's 400, an
    unknown jurisdiction 404, a change made meanwhile 409."""
    with Session(get_engine()) as session:
        try:
            return operation(session)
        except jurisdiction_templates.TemplateProblemsError as exc:  # DIAAT-344: which box
            raise HTTPException(
                status_code=400,
                detail={"code": TEMPLATE_NOT_READY, "message": str(exc), "problems": exc.problems},
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


def _jurisdiction_exists(code: str) -> bool:
    with Session(get_engine()) as session:
        return session.exec(select(Jurisdiction.id).where(Jurisdiction.code == code.strip().upper())).first() is not None


def _may_assign(admin_user: User, member_role: str) -> None:
    """Making someone a jurisdiction's ``admin`` (a JurisdictionAdministrator) is for those who may
    assign that role (a JurisdictionSuperAdministrator), not for a JurisdictionAdministrator."""
    if member_role == ROLE_ADMIN:
        ensure_permission(admin_user, "jurisdiction_admin.assign")


@router.get("/jurisdictions", response_model=list[JurisdictionAdminRecord])
def admin_list_jurisdictions(
    admin_user: User = Depends(_JURISDICTION_ADMIN),
) -> list[JurisdictionAdminRecord]:
    """The jurisdictions the caller manages, with their settings and member count: every one for
    a JurisdictionSuperAdministrator, only the one they are working in for a
    JurisdictionAdministrator."""
    scope = jurisdiction_scope(admin_user)
    return [
        JurisdictionAdminRecord(**row)
        for row in _admin_session_run(jurisdictions_admin.jurisdictions_overview)
        if scope is None or row["code"] in scope
    ]


@router.get("/jurisdictions/wording-keys", response_model=WordingKeysResponse)
def admin_jurisdiction_wording_keys(
    admin_user: User = Depends(_JURISDICTION_ADMIN),
) -> WordingKeysResponse:
    """The screen text a jurisdiction can set, with what each is for."""
    return WordingKeysResponse(keys=WORDING_KEYS)


@router.put("/jurisdictions/{code}", response_model=JurisdictionAdminRecord)
def admin_put_jurisdiction(
    code: str,
    body: JurisdictionWrite,
    admin_user: User = Depends(_JURISDICTION_ADMIN),
) -> JurisdictionAdminRecord:
    """Create or update a jurisdiction. Its settings are validated first (400); a
    stale ``version`` is refused (409). Updating needs the jurisdiction to be in the caller's
    scope; creating one needs ``jurisdiction.create`` too (403 ``permission_denied``)."""
    if _jurisdiction_exists(code):
        assert_jurisdiction_in_scope(admin_user, code)
    else:
        ensure_permission(admin_user, "jurisdiction.create")
    created = False

    def put(session: Session) -> dict:
        nonlocal created
        outcome = jurisdictions_admin.put_jurisdiction(
            session,
            code,
            body.name,
            body.settings,
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
            "template": settings.get("template"),
            "features": settings.get("features"),
            "wording_keys": sorted(settings.get("wording", {})),
        },
    )
    return JurisdictionAdminRecord(**record)


@router.get("/jurisdictions/{code}/members", response_model=list[JurisdictionMemberRecord])
def admin_list_jurisdiction_members(
    code: str,
    admin_user: User = Depends(_JURISDICTION_ADMIN),
) -> list[JurisdictionMemberRecord]:
    """A jurisdiction's members, including people added who have not signed in yet."""
    assert_jurisdiction_in_scope(admin_user, code)
    return [
        JurisdictionMemberRecord(**m) for m in _admin_session_run(lambda s: jurisdictions_admin.members_of(s, code))
    ]


@router.post("/jurisdictions/{code}/members", response_model=JurisdictionMemberRecord, status_code=201)
def admin_add_jurisdiction_member(
    code: str,
    body: JurisdictionMemberWrite,
    admin_user: User = Depends(_JURISDICTION_ADMIN),
) -> JurisdictionMemberRecord:
    """Add someone by their sign-in email, or change their role. Someone who has not
    signed in yet joins when they first do (pending). Making someone an ``admin`` needs
    ``jurisdiction_admin.assign``."""
    assert_jurisdiction_in_scope(admin_user, code)
    _may_assign(admin_user, body.role)
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
    admin_user: User = Depends(_JURISDICTION_ADMIN),
) -> None:
    """Remove someone from a jurisdiction (by email, as listed). Removing an ``admin`` needs
    ``jurisdiction_admin.assign``."""
    assert_jurisdiction_in_scope(admin_user, code)
    for member in _admin_session_run(lambda s: jurisdictions_admin.members_of(s, code)):
        if member["email"] == email.strip().lower():
            _may_assign(admin_user, member["role"])
    _admin_session_run(lambda s: jurisdictions_admin.remove_member(s, code, email))
    _audit_jurisdiction_change(
        "membership.remove", admin_user, {"code": code.strip().upper(), **_member_target(email)}
    )


# ---------------------------------------------------------------------------
# A jurisdiction's Word templates (DIAAT-344): ``templates.manage``, in the jurisdiction the
# caller works in (a JurisdictionSuperAdministrator: any)
# ---------------------------------------------------------------------------
# Uploaded as the raw request body with Content-Type exactly application/octet-stream,
# like the browser's other uploads: Front Door's WAF blocks multipart bodies, and lets
# this one through by a custom Allow rule (azure-platform-terraform). The file's name
# comes in X-Upload-Filename (URL-encoded).

UPLOAD_CONTENT_TYPE = "application/octet-stream"
_TEMPLATE_ADMIN = require_permission("templates.manage")


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
    admin_user: User = Depends(_TEMPLATE_ADMIN),
) -> list[TemplateAdminRecord]:
    """A jurisdiction's templates, each with its boxes, fields and what is missing."""
    assert_jurisdiction_in_scope(admin_user, code)
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
    admin_user: User = Depends(_TEMPLATE_ADMIN),
) -> TemplateAdminRecord:
    """Upload a Word template: a new one (201), with a field proposed for each box, or
    with ``replace`` and its ``version`` a new file for an existing one, keeping the
    fields of the boxes it still has. A file BenchNotes cannot use is refused (400)."""
    assert_jurisdiction_in_scope(admin_user, code)
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
    admin_user: User = Depends(_TEMPLATE_ADMIN),
) -> TemplateAdminRecord:
    """Save a template's name, fields and whether judges can choose it. Refused (400,
    ``template_not_ready``, with ``problems`` naming each box) unless every box has a
    complete field; 409 if it changed since ``version``."""
    assert_jurisdiction_in_scope(admin_user, code)
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


@router.delete("/jurisdictions/{code}/templates/{template_id}", status_code=204)
def admin_delete_template(
    code: str,
    template_id: UUID,
    admin_user: User = Depends(_TEMPLATE_ADMIN),
) -> None:
    """Delete a template. Documents already made from it are kept."""
    assert_jurisdiction_in_scope(admin_user, code)
    _admin_session_run(lambda s: jurisdiction_templates.delete_template(s, code, template_id))
    _audit_template_change("template.delete", admin_user, code, str(template_id), {})
