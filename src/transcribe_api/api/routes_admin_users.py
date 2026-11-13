"""Users, role assignments and the role-change history, for administrators (DIAAT-383, DIAAT-391,
DIAAT-394), under ``/api/admin``.

* ``GET /admin/users``: the users who have signed in (and those added by email who have not),
  paged and searchable, with their assignments. ``users.manage`` (a JurisdictionAdministrator: its
  jurisdiction's users), or ``jurisdiction_admin.assign`` / ``jurisdiction_super_admin.assign`` (to find
  whom to appoint: everyone, with only the role the caller appoints).
* ``POST /admin/assignments``, ``DELETE /admin/assignments/{id}``: give and take away a role,
  by the rules in domain/user_admin.py (403 ``assignment_not_allowed``, 403
  ``jurisdiction_not_in_scope``). ``users.manage``, ``jurisdiction_admin.assign`` or
  ``jurisdiction_super_admin.assign``: which of them is enough depends on the role (the rules decide).
* ``POST /admin/users/{id}/deactivate`` and ``/reactivate``: ``users.manage`` (a JurisdictionAdministrator:
  users whose roles are all in its jurisdiction) or ``accounts.manage`` (the JurisdictionSuperAdministrator:
  any account but its own). Nobody's reaches an account that holds SystemAdministrator (DIAAT-407).
* ``GET /admin/role-history``: ``role_audit.read`` (a JurisdictionAdministrator: its jurisdiction's; the
  JurisdictionSuperAdministrator: all of it). ``user=`` narrows it to the changes made to one user.

Every change is audited with the actor, the target and the change (domain/user_admin.py). Plain
defs: the database work is synchronous, so FastAPI runs them in the threadpool.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlmodel import Session

from transcribe_api.domain import user_admin
from transcribe_api.domain.auth.dependencies_dictation import require_permission
from transcribe_api.domain.models_dictation import User
from transcribe_api.runtime import db_connection

router = APIRouter(prefix="/admin", tags=["admin"])


class JurisdictionOut(BaseModel):
    code: str
    name: str


class AssignmentOut(BaseModel):
    id: UUID
    jurisdiction: JurisdictionOut | None
    role: str
    # In the jurisdiction's test group (DIAAT-345): sees it while it is at stage "test".
    tester: bool = False


class AdminUserOut(BaseModel):
    # For a row with ``pending`` true, not a user's id: the id of its first assignment.
    id: UUID
    email: str
    name: str | None
    is_active: bool
    assignments: list[AssignmentOut]
    pending: bool
    # Whether the caller may deactivate or reactivate this account: false for its own, a pending row,
    # one outside a JurisdictionAdministrator's reach, and one that holds SystemAdministrator (DIAAT-407).
    can_set_active: bool


class AdminUsersResponse(BaseModel):
    items: list[AdminUserOut]
    total: int
    page: int
    page_size: int


class AssignmentCreate(BaseModel):
    user_id: UUID | None = None
    email: str | None = Field(default=None, max_length=320)
    # null: the global role (JurisdictionSuperAdministrator).
    jurisdiction: str | None = Field(default=None, max_length=16)
    role: str = Field(max_length=64)
    # Judge or LegalTextManager only: in the jurisdiction's test group (DIAAT-345).
    tester: bool = False


class AssignmentCreated(AssignmentOut):
    # The user it belongs to, or null while it waits for someone who has not signed in
    # (then ``email`` is the address it was made for).
    user_id: UUID | None
    email: str | None
    pending: bool


def _jurisdiction(code: str | None, name: str | None) -> JurisdictionOut | None:
    return JurisdictionOut(code=code, name=name or code) if code else None


def _assignment(view: user_admin.AssignmentView) -> AssignmentOut:
    return AssignmentOut(
        id=view.id,
        jurisdiction=_jurisdiction(view.jurisdiction_code, view.jurisdiction_name),
        role=view.role,
        tester=view.tester,
    )


@router.get("/users", response_model=AdminUsersResponse)
def list_admin_users(
    response: Response,
    search: str | None = Query(default=None, max_length=200),
    jurisdiction: str | None = Query(default=None, max_length=16),
    role: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    admin_user: User = Depends(require_permission("users.manage", "jurisdiction_admin.assign", "jurisdiction_super_admin.assign")),
) -> AdminUsersResponse:
    """Users with their assignments, ordered by email, then the people added by email who have
    not signed in (``pending``). A JurisdictionAdministrator lists the jurisdiction it acts in; a
    ``search`` of three characters or more matches any user who has signed in, so they can be
    added (their assignments elsewhere are not shown). A JurisdictionSuperAdministrator or
    SystemAdministrator lists everyone. ``jurisdiction=`` and ``role=`` narrow the list to those who
    hold a role there."""
    response.headers["Cache-Control"] = "no-store"
    with Session(db_connection.get_engine()) as session:
        result = user_admin.list_users(
            session, admin_user, search=search, jurisdiction=jurisdiction, role=role, page=page, page_size=page_size
        )
    return AdminUsersResponse(
        items=[
            AdminUserOut(
                id=row.id,
                email=row.email,
                name=row.name,
                is_active=row.is_active,
                assignments=[_assignment(a) for a in row.assignments],
                pending=row.pending,
                can_set_active=row.can_set_active,
            )
            for row in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


_ASSIGN = require_permission("users.manage", "jurisdiction_admin.assign", "jurisdiction_super_admin.assign")


@router.post("/assignments", response_model=AssignmentCreated, status_code=201)
def create_assignment(
    body: AssignmentCreate,
    admin_user: User = Depends(_ASSIGN),
) -> AssignmentCreated:
    """Give a user (``user_id``), or an email (a pending assignment, claimed when they first sign
    in), a role in a jurisdiction. 400 for an invalid assignment, 403 ``assignment_not_allowed`` or
    ``jurisdiction_not_in_scope`` for what the rules refuse, 404 for an unknown user or jurisdiction,
    409 ``already_assigned``."""
    with Session(db_connection.get_engine()) as session:
        view = user_admin.create_assignment(
            session,
            admin_user,
            user_id=body.user_id,
            email=body.email,
            jurisdiction_code=body.jurisdiction,
            role=body.role,
            tester=body.tester,
        )
    return AssignmentCreated(
        **_assignment(view).model_dump(), user_id=view.user_id, email=view.email, pending=view.pending
    )


@router.delete("/assignments/{assignment_id}", status_code=204)
def delete_assignment(
    assignment_id: UUID,
    admin_user: User = Depends(_ASSIGN),
) -> Response:
    """Take a role away: an assignment of a user, or a pending one (by the id the user list gave
    it). 404 for an unknown id; 403 for what the rules refuse."""
    with Session(db_connection.get_engine()) as session:
        user_admin.delete_assignment(session, admin_user, assignment_id)
    return Response(status_code=204)


@router.post("/users/{user_id}/deactivate", status_code=204)
def deactivate_user(
    user_id: UUID,
    admin_user: User = Depends(require_permission("users.manage", "accounts.manage")),
) -> Response:
    """Deactivate a user: they are refused at their next request, and cannot sign in again until
    reactivated. Nobody deactivates themselves, or an account that holds SystemAdministrator. Idempotent."""
    with Session(db_connection.get_engine()) as session:
        user_admin.set_user_active(session, admin_user, user_id, active=False)
    return Response(status_code=204)


@router.post("/users/{user_id}/reactivate", status_code=204)
def reactivate_user(
    user_id: UUID,
    admin_user: User = Depends(require_permission("users.manage", "accounts.manage")),
) -> Response:
    """Reactivate a deactivated user. Idempotent."""
    with Session(db_connection.get_engine()) as session:
        user_admin.set_user_active(session, admin_user, user_id, active=True)
    return Response(status_code=204)


class PersonRef(BaseModel):
    # null, with the email "system administrator", for a change made through a SystemAdministrator's
    # own permissions: it is not named in the app.
    id: str | None
    email: str


class TargetRef(BaseModel):
    id: str | None
    email: str


class ActorContext(BaseModel):
    jurisdiction: str | None
    role: str


class RoleHistoryItem(BaseModel):
    id: UUID
    timestamp: datetime
    actor: PersonRef
    actor_context: ActorContext | None
    target: TargetRef
    change: str
    jurisdiction: str | None
    role: str | None


class RoleHistoryResponse(BaseModel):
    items: list[RoleHistoryItem]
    total: int


@router.get("/role-history", response_model=RoleHistoryResponse)
def get_role_history(
    response: Response,
    jurisdiction: str | None = Query(default=None, max_length=16),
    user: UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    admin_user: User = Depends(require_permission("role_audit.read")),
) -> RoleHistoryResponse:
    """Who changed whose roles, newest first: assignments added and removed, users deactivated and
    reactivated, jurisdictions left, access requests decided. A JurisdictionAdministrator reads its
    jurisdiction's; the JurisdictionSuperAdministrator every jurisdiction's and the global ones
    (``jurisdiction=`` narrows it). ``user=``: only the changes made to that user."""
    response.headers["Cache-Control"] = "no-store"
    with Session(db_connection.get_engine()) as session:
        result = user_admin.role_history(
            session, admin_user, jurisdiction=jurisdiction, user_id=user, page=page, page_size=page_size
        )
    return RoleHistoryResponse(
        items=[
            RoleHistoryItem(
                id=entry.id,
                timestamp=entry.timestamp,
                actor=PersonRef(id=entry.actor_id, email=entry.actor_email),
                actor_context=(
                    ActorContext(jurisdiction=entry.actor_jurisdiction, role=entry.actor_role) if entry.actor_role else None
                ),
                target=TargetRef(id=entry.target_id, email=entry.target_email),
                change=entry.change,
                jurisdiction=entry.jurisdiction,
                role=entry.role,
            )
            for entry in result.items
        ],
        total=result.total,
    )
