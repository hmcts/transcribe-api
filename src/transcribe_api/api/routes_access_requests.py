"""Access requests (DIAAT-396, DIAAT-398), under ``/api``.

The requester's side works for any signed-in user, with or without a role or a context (a user
with no role is who it is for): ``POST /access-requests``, ``GET /access-requests/mine``,
``POST /access-requests/{id}/withdraw``. The approver's side needs ``access.approve`` (a
JurisdictionAdministrator of the jurisdiction, or a JurisdictionSuperAdministrator; a
SystemAdministrator is refused): ``GET /admin/access-requests``,
``POST /admin/access-requests/{id}/approve`` and ``/reject``. The rules are
domain/access_requests.py; the emails (sent after the response, never failing it) are
domain/notify/access_request_emails.py. Plain defs: the work is synchronous, so FastAPI runs
them in the threadpool.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status
from pydantic import BaseModel, Field, StringConstraints
from sqlmodel import Session

from transcribe_api.domain import access_requests
from transcribe_api.domain.auth.dependencies_dictation import require_permission, signed_in_endpoint
from transcribe_api.domain.auth.sessions import AccessRequestOut
from transcribe_api.domain.models_dictation import User
from transcribe_api.domain.notify import access_request_emails
from transcribe_api.runtime import db_connection

router = APIRouter(tags=["access-requests"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])


class AccessRequestCreate(BaseModel):
    jurisdiction: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16)]
    # Which roles may be asked for is the API's to say (400 ``role_not_requestable``).
    roles: list[Annotated[str, StringConstraints(max_length=64)]] = Field(max_length=8)


@router.post("/access-requests", response_model=AccessRequestOut, status_code=201)
def create_access_request(
    body: AccessRequestCreate,
    response: Response,
    background: BackgroundTasks,
    user: User = Depends(signed_in_endpoint),  # noqa: B008
) -> AccessRequestOut:
    """Ask for Judge and/or LegalTextManager in a jurisdiction. 400 ``role_not_requestable`` for
    any other role, 404 for an unknown or disabled jurisdiction, 409 ``duplicate_request`` for an
    identical pending request and ``already_assigned`` for a role already held there. The people
    who can approve it are emailed after the response."""
    response.headers["Cache-Control"] = "no-store"
    with Session(db_connection.get_engine()) as session:
        created = access_requests.create_request(
            session, user.db_user, jurisdiction_code=body.jurisdiction, roles=body.roles
        )
    background.add_task(access_request_emails.notify_approvers, created.id)
    return created


@router.get("/access-requests/mine", response_model=list[AccessRequestOut])
def my_access_requests(
    response: Response,
    user: User = Depends(signed_in_endpoint),  # noqa: B008
) -> list[AccessRequestOut]:
    """Every request the signed-in user has made, newest first, with its status."""
    response.headers["Cache-Control"] = "no-store"
    with Session(db_connection.get_engine()) as session:
        return access_requests.my_requests(session, user.db_user)


@router.post("/access-requests/{request_id}/withdraw", response_model=AccessRequestOut)
def withdraw_access_request(
    request_id: UUID,
    response: Response,
    user: User = Depends(signed_in_endpoint),  # noqa: B008
) -> AccessRequestOut:
    """Withdraw one of the user's own requests that is still pending (404 for anyone else's,
    409 ``request_not_pending`` after that). No email is sent."""
    response.headers["Cache-Control"] = "no-store"
    with Session(db_connection.get_engine()) as session:
        return access_requests.withdraw_request(session, user.db_user, request_id)


class Requester(BaseModel):
    id: UUID
    email: str
    name: str | None


class QueuedAccessRequest(AccessRequestOut):
    requester: Requester


class DecisionBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@admin_router.get("/access-requests", response_model=list[QueuedAccessRequest])
def list_access_requests(
    response: Response,
    status_: Literal["pending", "approved", "rejected", "withdrawn", "all"] = Query(default="pending", alias="status"),
    admin_user: User = Depends(require_permission("access.approve")),  # noqa: B008
) -> list[QueuedAccessRequest]:
    """The approvals queue: the requests of the jurisdiction the approver acts in (every one for a
    JurisdictionSuperAdministrator), by default the pending ones, oldest first."""
    response.headers["Cache-Control"] = "no-store"
    with Session(db_connection.get_engine()) as session:
        queued = access_requests.list_for_approver(session, admin_user, status=status_)
    return [
        QueuedAccessRequest(
            **item.request.model_dump(),
            requester=Requester(id=item.requester_id, email=item.requester_email, name=item.requester_name),
        )
        for item in queued
    ]


def _decide(
    request_id: UUID, body: DecisionBody | None, admin_user: User, background: BackgroundTasks, *, approve: bool
) -> AccessRequestOut:
    with Session(db_connection.get_engine()) as session:
        decided = access_requests.decide_request(
            session, admin_user, request_id, approve=approve, reason=body.reason if body else None
        )
    background.add_task(access_request_emails.notify_requester, decided.id)
    return decided


@admin_router.post("/access-requests/{request_id}/approve", response_model=AccessRequestOut, status_code=status.HTTP_200_OK)
def approve_access_request(
    request_id: UUID,
    background: BackgroundTasks,
    body: DecisionBody | None = None,
    admin_user: User = Depends(require_permission("access.approve")),  # noqa: B008
) -> AccessRequestOut:
    """Approve a pending request: the requested roles are assigned in the same transaction. 403
    ``jurisdiction_not_in_scope`` for another jurisdiction's request, 409 ``request_not_pending``
    once decided. The requester is emailed after the response."""
    return _decide(request_id, body, admin_user, background, approve=True)


@admin_router.post("/access-requests/{request_id}/reject", response_model=AccessRequestOut)
def reject_access_request(
    request_id: UUID,
    background: BackgroundTasks,
    body: DecisionBody | None = None,
    admin_user: User = Depends(require_permission("access.approve")),  # noqa: B008
) -> AccessRequestOut:
    """Reject a pending request (nothing is assigned); ``reason`` is optional. As ``approve``."""
    return _decide(request_id, body, admin_user, background, approve=False)
