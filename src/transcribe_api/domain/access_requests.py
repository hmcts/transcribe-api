"""Access requests (DIAAT-396): a user asks for Judge and/or LegalTextManager in a jurisdiction, and
a JurisdictionAdministrator of it approves or rejects (DIAAT-406). Anyone may ask: someone with no
role, or someone who holds roles and wants one in another jurisdiction (or another role in one of theirs).

* Only ``REQUESTABLE_ROLES`` can be asked for (400 ``role_not_requestable``); the jurisdiction must
  exist and be enabled (404 ``jurisdiction_not_found``); a user already holding a requested role
  there gets 409 ``already_assigned``; an identical request still pending gets 409
  ``duplicate_request``.
* Approving writes the requested assignments, idempotently (a role the user already holds is
  left as it is), **in the same transaction** as the request's new status: if the write fails, the
  request stays pending. A request is decided once: 409 ``request_not_pending`` after that.
* Nobody decides their own request (403 ``own_request``); a request for a jurisdiction that has been
  disabled since cannot be approved (409 ``jurisdiction_disabled``); a user has at most
  ``MAX_PENDING_REQUESTS`` waiting (409 ``too_many_pending_requests``).
* The approver acts in the request's jurisdiction: 403 ``jurisdiction_not_in_scope`` otherwise.
  ``access.approve`` is the JurisdictionAdministrator's only: a JurisdictionSuperAdministrator and a
  SystemAdministrator are refused by the permission check.
* Every request, approval, rejection and withdrawal is audited (``access_request.created`` ...);
  approvals and rejections are in the role-change history (domain/user_admin.py), one row per role.

The functions take the caller's session and commit their own change. Emails are sent by the routes
afterwards (domain/notify/access_request_emails.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from transcribe_api.domain import assignments as assignment_service
from transcribe_api.domain import user_admin
from transcribe_api.domain.audit import stage_audit
from transcribe_api.domain.auth.authorisation import jurisdiction_in_scope, jurisdiction_scope
from transcribe_api.domain.auth.context import error
from transcribe_api.domain.auth.permissions import REQUESTABLE_ROLES, STORED_ROLES
from transcribe_api.domain.auth.sessions import AccessRequestOut, JurisdictionRef
from transcribe_api.domain.models_dictation import (
    AccessRequest,
    AccessRequestRole,
    Jurisdiction,
    RoleAssignment,
    User,
)

logger = logging.getLogger(__name__)

ACCESS_REQUEST_STATUSES = ("pending", "approved", "rejected", "withdrawn")
# How many requests one user may have waiting at once (409 ``too_many_pending_requests``): each one
# emails every approver of the jurisdiction.
MAX_PENDING_REQUESTS = 5
# How many requests one user may make in a day, whatever became of them (429 ``too_many_requests``). The cap
# on waiting ones alone is no limit on emails: ask, withdraw, ask again would send every approver a message
# each time and use up the Notify allowance.
MAX_REQUESTS_PER_DAY = 10
REQUEST_WINDOW = timedelta(hours=24)


def _roles_of(session: Session, request_ids: list[UUID]) -> dict[UUID, list[str]]:
    roles: dict[UUID, list[str]] = {request_id: [] for request_id in request_ids}
    if request_ids:
        for request_id, role in session.exec(
            select(AccessRequestRole.access_request_id, AccessRequestRole.role).where(
                AccessRequestRole.access_request_id.in_(request_ids)  # type: ignore[attr-defined]
            )
        ).all():
            roles[request_id].append(role)
    return {request_id: sorted(found, key=STORED_ROLES.index) for request_id, found in roles.items()}


def to_out(request: AccessRequest, jurisdiction: Jurisdiction, roles: list[str]) -> AccessRequestOut:
    return AccessRequestOut(
        id=request.id,
        jurisdiction=JurisdictionRef(code=jurisdiction.code, name=jurisdiction.name),
        roles=roles,
        status=request.status,  # type: ignore[arg-type]
        created_at=request.created_at,
        decided_at=request.decided_at,
        reason=request.reason,
    )


def _out(session: Session, request: AccessRequest) -> AccessRequestOut:
    jurisdiction = session.get(Jurisdiction, request.jurisdiction_id)
    return to_out(request, jurisdiction, _roles_of(session, [request.id])[request.id])  # type: ignore[arg-type]


def _audit(
    session: Session, action: str, actor_id: UUID, request: AccessRequest, jurisdiction: Jurisdiction, roles: list[str]
) -> None:
    """Add the audit row to the session: committed with the change, or the change is not made."""
    stage_audit(
        session,
        action,
        actor_id=actor_id,
        outcome="success",
        object_type="access_request",
        object_id=request.id,
        detail={"jurisdiction": jurisdiction.code, "roles": roles},
    )


# ---------------------------------------------------------------------------
# The requester's side
# ---------------------------------------------------------------------------


def create_request(session: Session, user: User, *, jurisdiction_code: str, roles: list[str]) -> AccessRequestOut:
    asked = sorted(set(roles))
    if not asked:
        raise error(400, "roles_required", "Ask for at least one role.")
    not_requestable = [role for role in asked if role not in REQUESTABLE_ROLES]
    if not_requestable:
        raise error(
            400, "role_not_requestable", f"You can only ask for {' and '.join(REQUESTABLE_ROLES)}."
        )
    jurisdiction = session.exec(
        select(Jurisdiction).where(Jurisdiction.code == jurisdiction_code.strip().upper(), Jurisdiction.enabled)
    ).first()
    if jurisdiction is None:
        raise error(404, "jurisdiction_not_found", "There is no such jurisdiction.")

    # One request at a time per user, so two identical requests sent together cannot both pass
    # the duplicate check (a row lock on PostgreSQL; SQLite has no lock to take).
    session.exec(select(User.id).where(User.id == user.id).with_for_update()).first()

    held = session.exec(
        select(RoleAssignment.role).where(
            RoleAssignment.user_id == user.id,
            RoleAssignment.jurisdiction_id == jurisdiction.id,
            RoleAssignment.role.in_(asked),  # type: ignore[attr-defined]
        )
    ).all()
    if held:
        raise error(409, "already_assigned", f"You already hold {' and '.join(sorted(held))} in {jurisdiction.code}.")
    pending = session.exec(
        select(AccessRequest).where(
            AccessRequest.user_id == user.id,
            AccessRequest.jurisdiction_id == jurisdiction.id,
            AccessRequest.status == "pending",
        )
    ).all()
    pending_roles = _roles_of(session, [p.id for p in pending])
    if any(set(found) == set(asked) for found in pending_roles.values()):
        raise error(409, "duplicate_request", "You have already asked for that, and it is waiting to be decided.")
    waiting = session.exec(
        select(func.count(AccessRequest.id)).where(  # type: ignore[arg-type]
            AccessRequest.user_id == user.id, AccessRequest.status == "pending"
        )
    ).one()
    if waiting >= MAX_PENDING_REQUESTS:
        raise error(
            409,
            "too_many_pending_requests",
            f"You already have {MAX_PENDING_REQUESTS} requests waiting to be decided: withdraw one first.",
        )

    recent = session.exec(
        select(func.count(AccessRequest.id)).where(  # type: ignore[arg-type]
            AccessRequest.user_id == user.id, AccessRequest.created_at >= datetime.now(UTC) - REQUEST_WINDOW
        )
    ).one()
    if recent >= MAX_REQUESTS_PER_DAY:
        raise error(
            429,
            "too_many_requests",
            f"You have already asked {MAX_REQUESTS_PER_DAY} times in the last day: try again tomorrow, "
            "or ask an administrator directly.",
        )

    request = AccessRequest(user_id=user.id, jurisdiction_id=jurisdiction.id)
    session.add(request)
    session.flush()
    for role in asked:
        session.add(AccessRequestRole(access_request_id=request.id, role=role))
    _audit(session, "access_request.created", user.id, request, jurisdiction, asked)
    session.commit()
    session.refresh(request)
    return to_out(request, jurisdiction, asked)


def my_requests(session: Session, user: User) -> list[AccessRequestOut]:
    """Every request the user has made, newest first."""
    rows = session.exec(
        select(AccessRequest, Jurisdiction)
        .join(Jurisdiction, Jurisdiction.id == AccessRequest.jurisdiction_id)
        .where(AccessRequest.user_id == user.id)
        .order_by(AccessRequest.created_at.desc())  # type: ignore[attr-defined]
    ).all()
    roles = _roles_of(session, [request.id for request, _ in rows])
    return [to_out(request, jurisdiction, roles[request.id]) for request, jurisdiction in rows]


def _locked(session: Session, request_id: UUID) -> AccessRequest | None:
    return session.exec(
        select(AccessRequest).where(AccessRequest.id == request_id).with_for_update().execution_options(populate_existing=True)
    ).first()


def withdraw_request(session: Session, user: User, request_id: UUID) -> AccessRequestOut:
    """Withdraw the user's own pending request. 404 for one that is not theirs (or unknown), 409
    ``request_not_pending`` once it is decided or withdrawn."""
    request = _locked(session, request_id)
    if request is None or request.user_id != user.id:
        session.rollback()
        raise error(404, "request_not_found", "There is no such request.")
    if request.status != "pending":
        session.rollback()
        raise error(409, "request_not_pending", f"That request is already {request.status}.")
    request.status = "withdrawn"
    request.decided_by = user.id
    request.decided_at = datetime.now(UTC)
    session.add(request)
    jurisdiction = session.get(Jurisdiction, request.jurisdiction_id)
    _audit(
        session, "access_request.withdrawn", user.id, request, jurisdiction,  # type: ignore[arg-type]
        _roles_of(session, [request.id])[request.id],
    )
    session.commit()
    session.refresh(request)
    return _out(session, request)


# ---------------------------------------------------------------------------
# The approver's side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueuedRequest:
    request: AccessRequestOut
    requester_id: UUID
    requester_email: str
    requester_name: str | None


def list_for_approver(session: Session, actor: object, *, status: str) -> list[QueuedRequest]:
    """The requests the actor may decide: those of the jurisdiction they act in. ``status``: one of the four, or ``all``. Pending ones oldest
    first (the queue), others newest first."""
    scope = jurisdiction_scope(actor)
    query = (
        select(AccessRequest, Jurisdiction, User)
        .join(Jurisdiction, Jurisdiction.id == AccessRequest.jurisdiction_id)
        .join(User, User.id == AccessRequest.user_id)
    )
    if scope is not None:
        if not scope:
            return []
        query = query.where(Jurisdiction.code.in_(sorted(scope)))  # type: ignore[attr-defined]
    if status != "all":
        query = query.where(AccessRequest.status == status)
    order = AccessRequest.created_at if status == "pending" else AccessRequest.created_at.desc()  # type: ignore[attr-defined]
    rows = session.exec(query.order_by(order)).all()
    roles = _roles_of(session, [request.id for request, _j, _u in rows])
    return [
        QueuedRequest(to_out(request, jurisdiction, roles[request.id]), user.id, user.email, user.name)
        for request, jurisdiction, user in rows
    ]


def decide_request(
    session: Session, actor: object, request_id: UUID, *, approve: bool, reason: str | None
) -> AccessRequestOut:
    """Approve (writing the assignments in the same transaction) or reject a pending request."""
    text = (reason or "").strip() or None
    for attempt in (1, 2):
        request = _locked(session, request_id)
        if request is None:
            session.rollback()
            raise error(404, "request_not_found", "There is no such request.")
        jurisdiction = session.get(Jurisdiction, request.jurisdiction_id)
        if not jurisdiction_in_scope(actor, jurisdiction.code):  # type: ignore[union-attr]
            session.rollback()
            raise user_admin.out_of_scope(actor, jurisdiction.code)  # type: ignore[union-attr]
        if request.status != "pending":
            session.rollback()
            raise error(409, "request_not_pending", f"That request is already {request.status}.")
        if request.user_id == actor.id:  # type: ignore[attr-defined]
            session.rollback()
            raise user_admin.refuse(actor, "own_request", "You cannot decide your own access request.")
        if approve and not jurisdiction.enabled:  # type: ignore[union-attr]
            session.rollback()
            raise error(409, "jurisdiction_disabled", "That jurisdiction is disabled: nobody can be given a role there.")
        roles = _roles_of(session, [request.id])[request.id]
        try:
            if approve:
                for role in roles:
                    assignment_service.ensure_assignment(
                        session,
                        role=role,
                        jurisdiction_id=jurisdiction.id,  # type: ignore[union-attr]
                        user_id=request.user_id,
                        granted_by=actor.id,  # type: ignore[attr-defined]
                    )
            request.status = "approved" if approve else "rejected"
            request.decided_by = actor.id  # type: ignore[attr-defined]
            request.decided_at = datetime.now(UTC)
            request.reason = text
            session.add(request)
            # One history row per role, in the same transaction as the decision and the assignments.
            for role in roles:
                user_admin.audit_role_change(
                    session,
                    user_admin.CHANGE_REQUEST_APPROVED if approve else user_admin.CHANGE_REQUEST_REJECTED,
                    actor,
                    jurisdiction=jurisdiction,
                    target_user_id=request.user_id,
                    target_email=None,
                    role=role,
                    extra={"request_id": str(request.id)},
                )
            session.commit()
        except IntegrityError:
            # An assignment made by someone else at this moment: look again (once), when
            # ``ensure_assignment`` finds it.
            session.rollback()
            if attempt == 2:
                raise
            continue
        except Exception:
            session.rollback()  # the request stays pending, nothing of it is written
            raise
        break
    session.refresh(request)
    return to_out(request, jurisdiction, roles)  # type: ignore[arg-type]
