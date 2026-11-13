"""Users and role assignments, as the administration endpoints work on them (DIAAT-383).

Who may assign what is decided here, in one place, from what the caller's BenchNotes token
says they hold (the permissions of the role they act in only, domain/auth/authorisation.py): the rules of the contract's section 1.

* **Judge, LegalTextManager** in a jurisdiction: whoever holds ``users.manage`` there: a
  JurisdictionAdministrator, in the jurisdiction it acts in (403 ``jurisdiction_not_in_scope`` for another).
* **JurisdictionAdministrator**: whoever holds ``jurisdiction_admin.assign``: the
  JurisdictionSuperAdministrator only, in any jurisdiction (from the Jurisdictions screen).
* **JurisdictionSuperAdministrator** (global): whoever holds ``jurisdiction_super_admin.assign``,
  which only the SystemAdministrator does.
* **SystemAdministrator**: nobody. It is not a stored role.
* Giving a role to, or taking it from, **yourself** follows the same rules as for anyone else
  (DIAAT-406): a JurisdictionAdministrator may give itself Judge or LegalTextManager in its jurisdiction,
  a JurisdictionSuperAdministrator JurisdictionAdministrator anywhere, a SystemAdministrator
  JurisdictionSuperAdministrator. A Judge or LegalTextManager gives nobody a role (they raise an access
  request, which a JurisdictionAdministrator decides).
* Nobody takes away the role they are acting in, and nobody deactivates themselves.

A user holds any combination of roles in any number of jurisdictions. A JurisdictionAdministrator sees and
changes only the assignments in its own jurisdiction of a user who also holds roles elsewhere.

Every refusal is 403 ``assignment_not_allowed`` (or ``jurisdiction_not_in_scope``), audited as
``ACCESS_DENIED``. Every change is audited (``assignment.added``, ``assignment.removed``,
``user.deactivated``, ``user.reactivated``) with the actor (and, by the audit writer, the
jurisdiction and role they acted in), the target and the change; those rows are what
``GET /api/admin/role-history`` reads (``role_history``). An audit row has no contact details,
except that the email of someone who has not signed in yet is kept in its ``detail`` (there is
no user to name, and the history has to say who it was for).

The functions take the caller's session and commit their own change.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import distinct, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from transcribe_api.domain import assignments as assignment_service
from transcribe_api.domain.audit import ACCESS_DENIED, DENIED, record_audit, stage_audit
from transcribe_api.domain.auth import context as auth_context
from transcribe_api.domain.auth.authorisation import (
    JURISDICTION_NOT_IN_SCOPE,
    has_permission,
    jurisdiction_in_scope,
    jurisdiction_scope,
)
from transcribe_api.domain.auth.context import error
from transcribe_api.domain.auth.permissions import (
    GLOBAL_ROLES,
    ROLE_JUDGE,
    ROLE_JURISDICTION_ADMIN,
    ROLE_JURISDICTION_SUPER_ADMIN,
    ROLE_LEGAL_TEXT_MANAGER,
    ROLE_SYSTEM_ADMIN,
    STORED_ROLES,
    assignable_roles,
)
from transcribe_api.domain.models_dictation import AuditLog, Jurisdiction, RoleAssignment, User

logger = logging.getLogger(__name__)

ASSIGNMENT_NOT_ALLOWED = "assignment_not_allowed"

# The audit actions the role-change history is made of (contract section 3).
CHANGE_ASSIGNMENT_ADDED = "assignment.added"
CHANGE_ASSIGNMENT_REMOVED = "assignment.removed"
CHANGE_USER_DEACTIVATED = "user.deactivated"
CHANGE_USER_REACTIVATED = "user.reactivated"
CHANGE_MEMBERSHIP_LEAVE = "membership.leave"
CHANGE_REQUEST_APPROVED = "access_request.approved"
CHANGE_REQUEST_REJECTED = "access_request.rejected"
ROLE_CHANGE_ACTIONS = (
    CHANGE_ASSIGNMENT_ADDED,
    CHANGE_ASSIGNMENT_REMOVED,
    CHANGE_USER_DEACTIVATED,
    CHANGE_USER_REACTIVATED,
    CHANGE_MEMBERSHIP_LEAVE,
    CHANGE_REQUEST_APPROVED,
    CHANGE_REQUEST_REJECTED,
)

# A search shorter than this does not reach beyond the caller's own jurisdiction (below).
WIDE_SEARCH_MIN_LENGTH = 3

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def refuse(actor: object, code: str, message: str, *, detail: dict | None = None) -> HTTPException:
    """The 403 for a change the rules refuse, audited (as the permission checks audit theirs)."""
    record_audit(
        ACCESS_DENIED,
        actor_id=getattr(actor, "id", "unknown"),
        outcome=DENIED,
        object_type="assignment",
        detail={"reason": code, **(detail or {})},
    )
    return error(403, code, message)


def out_of_scope(actor: object, code: str) -> HTTPException:
    record_audit(
        ACCESS_DENIED,
        actor_id=getattr(actor, "id", "unknown"),
        outcome=DENIED,
        object_type="jurisdiction",
        detail={"reason": JURISDICTION_NOT_IN_SCOPE, "code": code},
    )
    return error(403, JURISDICTION_NOT_IN_SCOPE, "That jurisdiction is not the one you are working in.")


def jurisdiction_by_code(session: Session, code: str) -> Jurisdiction:
    row = session.exec(select(Jurisdiction).where(Jurisdiction.code == code.strip().upper())).first()
    if row is None:
        raise error(404, "jurisdiction_not_found", "There is no such jurisdiction.")
    return row


# ---------------------------------------------------------------------------
# Who may assign what
# ---------------------------------------------------------------------------


def check_may_change_assignment(
    actor: object, *, role: str, jurisdiction_code: str | None, target_user_id: UUID | None, adding: bool = False
) -> None:
    """Raise the 403 unless the actor may give (or take away) ``role`` in ``jurisdiction_code``
    (None for the global role) to the user ``target_user_id`` (None: someone who has not signed in).
    ``adding``: the role is being given (not taken away). Giving a role to yourself follows the same
    rules as giving it to anyone else: only a role strictly below the one you act in (DIAAT-403).
    The role and where it is held are valid (``check_assignable``); this is only about who may."""
    if role == ROLE_SYSTEM_ADMIN:
        raise refuse(actor, ASSIGNMENT_NOT_ALLOWED, "SystemAdministrator cannot be assigned.", detail={"role": role})
    me = getattr(actor, "id", None)
    is_self = target_user_id is not None and target_user_id == me

    ctx = auth_context.context_of(actor)
    if is_self and not adding and ctx is not None and ctx.role == role and (
        role in GLOBAL_ROLES or (ctx.jurisdiction or "").upper() == (jurisdiction_code or "")
    ):
        raise refuse(
            actor, ASSIGNMENT_NOT_ALLOWED, "You cannot remove the role you are acting in.", detail={"role": role}
        )

    if role == ROLE_JURISDICTION_SUPER_ADMIN:
        if not has_permission(actor, "jurisdiction_super_admin.assign"):
            raise refuse(
                actor,
                ASSIGNMENT_NOT_ALLOWED,
                "Only a SystemAdministrator assigns the JurisdictionSuperAdministrator role.",
                detail={"role": role},
            )
        return
    if role == ROLE_JURISDICTION_ADMIN:
        if not has_permission(actor, "jurisdiction_admin.assign"):
            raise refuse(
                actor,
                ASSIGNMENT_NOT_ALLOWED,
                "Only a JurisdictionSuperAdministrator assigns the JurisdictionAdministrator role.",
                detail={"role": role, "code": jurisdiction_code},
            )
        return
    # Judge and LegalTextManager: users.manage, in the jurisdiction.
    if not has_permission(actor, "users.manage"):
        raise refuse(
            actor,
            ASSIGNMENT_NOT_ALLOWED,
            "Only a JurisdictionAdministrator assigns the Judge and LegalTextManager roles.",
            detail={"role": role},
        )
    # (With no jurisdiction there is nothing to be in scope of: the caller says it is invalid.)
    if jurisdiction_code is not None and not jurisdiction_in_scope(actor, jurisdiction_code):
        raise out_of_scope(actor, jurisdiction_code)


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssignmentView:
    """An assignment as the endpoints show it: whose it is (a user, or an email not yet claimed)."""

    id: UUID
    role: str
    jurisdiction_code: str | None
    jurisdiction_name: str | None
    user_id: UUID | None
    email: str | None
    pending: bool
    # In the jurisdiction's test group (DIAAT-345): of a Judge or LegalTextManager.
    tester: bool = False


def _view(row: RoleAssignment, jurisdiction: Jurisdiction | None, user: User | None) -> AssignmentView:
    return AssignmentView(
        id=row.id,
        role=row.role,
        jurisdiction_code=jurisdiction.code if jurisdiction else None,
        jurisdiction_name=jurisdiction.name if jurisdiction else None,
        user_id=row.user_id,
        email=(user.email if user else row.pending_email),
        pending=row.user_id is None,
        tester=row.tester,
    )


def _users_by_email(session: Session, email: str) -> list[User]:
    """Every user row that signed in with that email, newest first. Normally one: more than one
    means the same address was claimed by two Entra accounts (an account deleted and recreated, a
    guest with a look-alike address), and which of them an assignment is for is not for the code to guess."""
    return list(
        session.exec(
            select(User).where(func.lower(User.email) == email).order_by(User.created_datetime.desc())  # type: ignore[attr-defined]
        ).all()
    )


def _by_system_permission(actor: object, role: str | None) -> bool:
    """Whether an assignment change of ``role`` by ``actor`` was made through the SystemAdministrator's own
    permissions: the actor is one and the role they act in (if any) could not have made it."""
    if role is None or not auth_context.sysadmin_of(actor):
        return False
    ctx = auth_context.context_of(actor)
    return role not in assignable_roles(ctx.role if ctx else None, sysadmin=False)


def audit_role_change(
    session: Session,
    action: str,
    actor: object,
    *,
    jurisdiction: Jurisdiction | None,
    target_user_id: UUID | None,
    target_email: str | None,
    role: str | None,
    extra: dict | None = None,
) -> None:
    """Add the audit row of a role change to ``session``, to be committed with the change itself (call it
    before the commit: fail-closed, ``audit.stage_audit``). Filed against the jurisdiction it concerns (the
    history of a JurisdictionAdministrator reads those), else against the user. The email is kept only
    for a target who has not signed in (no user to name)."""
    detail: dict = {
        "target_user_id": str(target_user_id) if target_user_id else None,
        "jurisdiction": jurisdiction.code if jurisdiction else None,
        "role": role,
        **(extra or {}),
    }
    if action in (CHANGE_ASSIGNMENT_ADDED, CHANGE_ASSIGNMENT_REMOVED) and _by_system_permission(actor, role):
        # The role history does not name the SystemAdministrator (it is not named in the app).
        detail["by_system"] = True
    if target_user_id is None and target_email:
        detail["target_email"] = target_email
    stage_audit(
        session,
        action,
        actor_id=actor.id,  # type: ignore[attr-defined]
        outcome="success",
        object_type="jurisdiction" if jurisdiction else "user",
        object_id=jurisdiction.id if jurisdiction else target_user_id,
        detail=detail,
    )


def create_assignment(
    session: Session,
    actor: object,
    *,
    user_id: UUID | None,
    email: str | None,
    jurisdiction_code: str | None,
    role: str,
    tester: bool = False,
) -> AssignmentView:
    """Give a user, or an email (pending until they first sign in), a role. 400 for what is not
    a valid assignment, 403 for what the rules refuse, 404 for an unknown user or jurisdiction,
    409 if they already hold it. ``tester``: in the jurisdiction's test group, who see it while it
    is at stage "test" (DIAAT-345); for a Judge or LegalTextManager only."""
    if (user_id is None) == (not (email or "").strip()):
        raise error(400, "invalid_target", "Name the user (user_id) or an email, not both and not neither.")
    if role != ROLE_SYSTEM_ADMIN and role not in STORED_ROLES:
        raise error(400, "invalid_role", "That is not a role that can be assigned.")
    target: User | None = None
    pending_email: str | None = None
    if user_id is not None:
        target = session.get(User, user_id)
        if target is None:
            raise error(404, "user_not_found", "There is no such user.")
    else:
        pending_email = assignment_service.normalise_email(email)
        if not _EMAIL.match(pending_email):
            raise error(400, "invalid_email", "That is not an email address.")
        # Someone who has signed in already will never "first sign in" again: assign to them. If
        # the address belongs to more than one user row, refuse rather than pick one (the newest
        # would be a look-alike's): assign to the person by user_id instead.
        found = _users_by_email(session, pending_email)
        if len(found) > 1:
            raise error(
                409,
                "ambiguous_email",
                "More than one user has signed in with that email address: choose the person from the user list.",
            )
        target = found[0] if found else None
        if target is not None:
            pending_email = None

    # What the rules say comes before what is wrong with the jurisdiction asked for: a caller who
    # may not assign the role at all is told that, whatever they sent with it.
    check_may_change_assignment(
        actor,
        role=role,
        jurisdiction_code=jurisdiction_code.strip().upper() if jurisdiction_code else None,
        target_user_id=target.id if target else None,
        adding=True,
    )
    jurisdiction = jurisdiction_by_code(session, jurisdiction_code) if jurisdiction_code else None
    if jurisdiction is not None and not jurisdiction.enabled:
        raise error(409, "jurisdiction_disabled", "That jurisdiction is disabled: nobody can be given a role there.")
    if role in GLOBAL_ROLES and jurisdiction is not None:
        raise error(400, "invalid_assignment", f"{role} is global: it has no jurisdiction.")
    if role not in GLOBAL_ROLES and jurisdiction is None:
        raise error(400, "invalid_assignment", f"{role} needs a jurisdiction.")
    if tester and role not in (ROLE_JUDGE, ROLE_LEGAL_TEXT_MANAGER):
        raise error(400, "invalid_assignment", "Only a judge or legal text manager is in a test group.")
    try:
        row = assignment_service.add_assignment(
            session,
            role=role,
            jurisdiction_id=jurisdiction.id if jurisdiction else None,
            user_id=target.id if target else None,
            email=pending_email,
            granted_by=actor.id,  # type: ignore[attr-defined]
            tester=tester,
        )
        # In the same transaction: if the audit row cannot be written the assignment is not made.
        audit_role_change(
            session,
            CHANGE_ASSIGNMENT_ADDED,
            actor,
            jurisdiction=jurisdiction,
            target_user_id=row.user_id,
            target_email=row.pending_email,
            role=role,
            extra={"assignment_id": str(row.id), **({"tester": True} if tester else {})},
        )
        session.commit()
    except assignment_service.DuplicateAssignmentError:
        session.rollback()
        raise error(409, "already_assigned", "They already hold that role there.") from None
    except IntegrityError:  # the same assignment made at the same moment
        session.rollback()
        raise error(409, "already_assigned", "They already hold that role there.") from None
    except assignment_service.AssignmentError as exc:
        session.rollback()
        raise error(400, "invalid_assignment", str(exc)) from None
    session.refresh(row)
    return _view(row, jurisdiction, target)


def delete_assignment(session: Session, actor: object, assignment_id: UUID) -> None:
    """Take a role away: an assignment of a user, or a pending one (by the id the user list
    gave it). 404 for an unknown id, 403 for what the rules refuse."""
    row = session.get(RoleAssignment, assignment_id)
    if row is None:
        raise error(404, "assignment_not_found", "There is no such assignment.")
    jurisdiction = session.get(Jurisdiction, row.jurisdiction_id) if row.jurisdiction_id else None
    check_may_change_assignment(
        actor,
        role=row.role,
        jurisdiction_code=jurisdiction.code if jurisdiction else None,
        target_user_id=row.user_id,
    )
    user_id, pending_email, role = row.user_id, row.pending_email, row.role
    session.delete(row)
    audit_role_change(
        session,
        CHANGE_ASSIGNMENT_REMOVED,
        actor,
        jurisdiction=jurisdiction,
        target_user_id=user_id,
        target_email=pending_email,
        role=role,
        extra={"assignment_id": str(assignment_id)},
    )
    session.commit()


# ---------------------------------------------------------------------------
# Deactivation
# ---------------------------------------------------------------------------


def _role_may_assign(actor: object, role: str) -> bool:
    """Whether the stored role the actor acts in may assign ``role`` (permissions.assignable_roles, without
    the SystemAdministrator's additions: they are for assigning, not for switching someone off)."""
    ctx = auth_context.context_of(actor)
    return role in assignable_roles(ctx.role if ctx else None, sysadmin=False)


def set_user_active(session: Session, actor: object, user_id: UUID, *, active: bool) -> bool:
    """Deactivate or reactivate a user (instead of deleting them); False if they already were.

    Nobody deactivates themselves (403 ``cannot_deactivate_self``). The JurisdictionSuperAdministrator
    (``accounts.manage``) reaches any account, administrators and users of several jurisdictions included
    (DIAAT-406). A JurisdictionAdministrator (``users.manage``) reaches only users whose roles are all in
    the jurisdiction it acts in (404 for any other: they are not in its list; 403 ``assignment_not_allowed``
    for one who also works elsewhere, since deactivation is across every jurisdiction), and never one who
    holds an administrator role."""
    target = session.get(User, user_id)
    if target is None:
        raise error(404, "user_not_found", "There is no such user.")
    if user_id == actor.id:  # type: ignore[attr-defined]
        raise refuse(actor, "cannot_deactivate_self", "You cannot deactivate your own account.")
    held = session.exec(
        select(RoleAssignment, Jurisdiction)
        .join(Jurisdiction, Jurisdiction.id == RoleAssignment.jurisdiction_id, isouter=True)
        .where(RoleAssignment.user_id == user_id)
    ).all()
    if not has_permission(actor, "accounts.manage"):
        scope = jurisdiction_scope(actor)
        in_scope = [row for row, j in held if j is not None and scope is not None and j.code in scope]
        if not in_scope:
            raise error(404, "user_not_found", "There is no such user.")
        if len(in_scope) != len(held):
            raise refuse(
                actor,
                ASSIGNMENT_NOT_ALLOWED,
                "That user has roles in other jurisdictions too: remove their roles here instead.",
            )
        for row, _j in held:
            if not _role_may_assign(actor, row.role):
                raise refuse(actor, ASSIGNMENT_NOT_ALLOWED, f"You cannot deactivate a {row.role}.")
    if target.is_active == active:
        return False
    target.is_active = active
    session.add(target)
    places = [j for _row, j in held]
    codes = sorted({j.code for j in places if j is not None})
    # Filed against the jurisdiction when it concerns exactly one (its administrators read that).
    single = places[0] if places and None not in places and len(codes) == 1 else None
    audit_role_change(
        session,
        CHANGE_USER_REACTIVATED if active else CHANGE_USER_DEACTIVATED,
        actor,
        jurisdiction=single,
        target_user_id=user_id,
        target_email=None,
        role=None,
        extra={"jurisdictions": codes},
    )
    session.commit()  # the change and its audit row, or neither
    return True


# ---------------------------------------------------------------------------
# The user list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRow:
    id: UUID
    email: str
    name: str | None
    is_active: bool
    assignments: list[AssignmentView]
    pending: bool


@dataclass(frozen=True)
class UserPage:
    items: list[UserRow]
    total: int
    page: int
    page_size: int


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def visible_scope(actor: object) -> frozenset[str] | None:
    """The jurisdictions whose users the actor lists: None for all, else the one they act in.

    A JurisdictionAdministrator (``users.manage``) lists its jurisdiction's. Whoever reaches the list only
    to appoint (a JurisdictionSuperAdministrator choosing a JurisdictionAdministrator, a SystemAdministrator a
    JurisdictionSuperAdministrator) searches everyone, but sees only the roles they appoint
    (``visible_roles``)."""
    if has_permission(actor, "users.manage"):
        return jurisdiction_scope(actor)
    return None


def visible_roles(actor: object) -> list[str] | None:
    """The roles whose assignments the actor sees in the list: None for every role (a
    JurisdictionAdministrator, in its jurisdiction), else only those it may assign."""
    if has_permission(actor, "users.manage"):
        return None
    ctx = auth_context.context_of(actor)
    return assignable_roles(ctx.role if ctx else None, auth_context.sysadmin_of(actor))


def list_users(
    session: Session,
    actor: object,
    *,
    search: str | None,
    jurisdiction: str | None,
    role: str | None,
    page: int,
    page_size: int,
) -> UserPage:
    """Users who have signed in, then the pending (email-only) assignments grouped by email.

    A JurisdictionSuperAdministrator or SystemAdministrator lists everyone, with only the assignments of
    the role they appoint (``jurisdiction=`` and ``role=`` narrow it to those holding a role there; a
    ``role=`` they do not appoint is 403). A JurisdictionAdministrator lists the
    users with a role in the jurisdiction it acts in; but a ``search`` of at least
    ``WIDE_SEARCH_MIN_LENGTH`` characters (and no ``jurisdiction=`` or ``role=``) matches the email
    of any user who has signed in, so someone can be added to the jurisdiction, and shows them with
    only their assignments there. ``jurisdiction=`` for one it does not act in is 403."""
    scope = visible_scope(actor)
    roles_shown = visible_roles(actor)
    if role is not None and roles_shown is not None and role not in roles_shown:
        raise refuse(actor, ASSIGNMENT_NOT_ALLOWED, "You cannot list the holders of that role.", detail={"role": role})
    needle = (search or "").strip().lower()
    explicit: Jurisdiction | None = None
    if jurisdiction:
        explicit = jurisdiction_by_code(session, jurisdiction)
        if scope is not None and explicit.code not in scope:
            raise out_of_scope(actor, explicit.code)

    scope_ids: list[UUID] | None = None
    if scope is not None:
        scope_ids = [
            j_id for j_id in session.exec(select(Jurisdiction.id).where(Jurisdiction.code.in_(sorted(scope)))).all()  # type: ignore[attr-defined]
        ] if scope else []
        if not scope_ids:
            return UserPage([], 0, page, page_size)
    filter_ids = [explicit.id] if explicit is not None else scope_ids  # None: no jurisdiction filter

    wide = scope is not None and explicit is None and role is None and len(needle) >= WIDE_SEARCH_MIN_LENGTH

    # --- users who have signed in
    users_q = select(User)
    count_q = select(func.count(User.id))  # type: ignore[arg-type]
    conditions = []
    if needle:
        conditions.append(func.lower(User.email).like(_like(needle), escape="\\"))
    if not wide and (filter_ids is not None or role is not None):
        held = select(RoleAssignment.user_id).where(RoleAssignment.user_id.is_not(None))  # type: ignore[union-attr]
        if filter_ids is not None:
            held = held.where(RoleAssignment.jurisdiction_id.in_(filter_ids))  # type: ignore[union-attr]
        if role is not None:
            held = held.where(RoleAssignment.role == role)
        elif roles_shown is not None:
            held = held.where(RoleAssignment.role.in_(roles_shown))  # type: ignore[attr-defined]
        conditions.append(User.id.in_(held))  # type: ignore[attr-defined]
    for condition in conditions:
        users_q = users_q.where(condition)
        count_q = count_q.where(condition)
    users_total = session.exec(count_q).one()

    # --- pending (email only), one row per email
    pending_conditions = [RoleAssignment.user_id.is_(None), RoleAssignment.pending_email.is_not(None)]  # type: ignore[union-attr]
    if filter_ids is not None:
        pending_conditions.append(RoleAssignment.jurisdiction_id.in_(filter_ids))  # type: ignore[union-attr]
    if role is not None:
        pending_conditions.append(RoleAssignment.role == role)
    elif roles_shown is not None:
        pending_conditions.append(RoleAssignment.role.in_(roles_shown))  # type: ignore[attr-defined]
    if needle:
        pending_conditions.append(RoleAssignment.pending_email.like(_like(needle), escape="\\"))  # type: ignore[union-attr]
    pending_emails_q = select(RoleAssignment.pending_email).where(*pending_conditions)
    pending_total = session.exec(
        select(func.count(distinct(RoleAssignment.pending_email))).where(*pending_conditions)
    ).one()

    start = (page - 1) * page_size
    users: list[User] = []
    if start < users_total:
        users = list(
            session.exec(
                users_q.order_by(func.lower(User.email), User.id).offset(start).limit(page_size)  # type: ignore[arg-type]
            ).all()
        )
    pending_emails: list[str] = []
    room = page_size - len(users)
    if room > 0 and pending_total:
        pending_emails = [
            e
            for e in session.exec(
                pending_emails_q.group_by(RoleAssignment.pending_email)
                .order_by(RoleAssignment.pending_email)
                .offset(max(0, start - users_total))
                .limit(room)
            ).all()
        ]

    # --- their assignments (for a jurisdiction administrator: only those in its jurisdiction)
    def visible(query):
        if scope_ids is not None:
            query = query.where(RoleAssignment.jurisdiction_id.in_(scope_ids))  # type: ignore[union-attr]
        if roles_shown is not None:
            query = query.where(RoleAssignment.role.in_(roles_shown))  # type: ignore[attr-defined]
        return query

    by_user: dict[UUID, list[AssignmentView]] = {u.id: [] for u in users}
    if users:
        rows = session.exec(
            visible(
                select(RoleAssignment, Jurisdiction)
                .join(Jurisdiction, Jurisdiction.id == RoleAssignment.jurisdiction_id, isouter=True)
                .where(RoleAssignment.user_id.in_(list(by_user)))  # type: ignore[union-attr]
            )
        ).all()
        for row, j in sorted(rows, key=lambda pair: (pair[1].name if pair[1] else "", STORED_ROLES.index(pair[0].role))):
            by_user[row.user_id].append(_view(row, j, None))
    by_email: dict[str, list[tuple[RoleAssignment, Jurisdiction | None]]] = {e: [] for e in pending_emails}
    if pending_emails:
        rows = session.exec(
            visible(
                select(RoleAssignment, Jurisdiction)
                .join(Jurisdiction, Jurisdiction.id == RoleAssignment.jurisdiction_id, isouter=True)
                .where(RoleAssignment.user_id.is_(None), RoleAssignment.pending_email.in_(pending_emails))  # type: ignore[union-attr]
            )
        ).all()
        for row, j in rows:
            by_email[row.pending_email].append((row, j))

    items = [
        UserRow(
            id=u.id,
            email=u.email,
            name=u.name,
            is_active=u.is_active,
            assignments=by_user[u.id],
            pending=False,
        )
        for u in users
    ]
    for email in pending_emails:
        rows = sorted(by_email[email], key=lambda pair: (pair[0].granted_at, str(pair[0].id)))
        if not rows:
            continue
        items.append(
            UserRow(
                # Not a user's id (nothing can be deactivated): the first of its assignments'.
                id=rows[0][0].id,
                email=email,
                name=None,
                is_active=True,
                assignments=[_view(row, j, None) for row, j in rows],
                pending=True,
            )
        )
    return UserPage(items, users_total + pending_total, page, page_size)


# ---------------------------------------------------------------------------
# The role-change history
# ---------------------------------------------------------------------------


# Shown for the actor of a change that only a SystemAdministrator acting through its own permissions
# can have made (see ``role_history``): the SystemAdministrator is not named in the app.
SYSTEM_ACTOR_LABEL = "system administrator"


@dataclass(frozen=True)
class HistoryEntry:
    id: UUID
    timestamp: datetime
    actor_id: str | None
    actor_email: str
    actor_jurisdiction: str | None
    actor_role: str | None
    target_id: str | None
    target_email: str
    change: str
    jurisdiction: str | None
    role: str | None


@dataclass(frozen=True)
class HistoryPage:
    items: list[HistoryEntry]
    total: int


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def role_history(
    session: Session,
    actor: object,
    *,
    jurisdiction: str | None,
    page: int,
    page_size: int,
    user_id: UUID | None = None,
) -> HistoryPage:
    """The role changes (``ROLE_CHANGE_ACTIONS``), newest first. A JurisdictionAdministrator reads those of
    the jurisdiction it acts in (403 for another via ``jurisdiction=``); the JurisdictionSuperAdministrator
    all of them, the global ones included, or one jurisdiction's with ``jurisdiction=``. ``user_id``: only
    the changes made to that user (who left a jurisdiction is the row's actor), those made to their email
    before they first signed in included (by the assignment they claimed)."""
    scope = jurisdiction_scope(actor)
    query = select(AuditLog).where(AuditLog.event_type.in_(ROLE_CHANGE_ACTIONS))  # type: ignore[attr-defined]
    count_query = select(func.count(AuditLog.id)).where(AuditLog.event_type.in_(ROLE_CHANGE_ACTIONS))  # type: ignore[attr-defined,arg-type]
    wanted_ids: list[UUID] | None = None
    if jurisdiction:
        row = jurisdiction_by_code(session, jurisdiction)
        if scope is not None and row.code not in scope:
            raise out_of_scope(actor, row.code)
        wanted_ids = [row.id]
    elif scope is not None:
        wanted_ids = [j_id for j_id in session.exec(select(Jurisdiction.id).where(Jurisdiction.code.in_(sorted(scope)))).all()] if scope else []  # type: ignore[attr-defined]
    if wanted_ids is not None:
        keep = (AuditLog.object_type == "jurisdiction") & AuditLog.object_id.in_([str(i) for i in wanted_ids])  # type: ignore[union-attr]
        query = query.where(keep)
        count_query = count_query.where(keep)
    if user_id is not None:
        claimed = select(AuditLog.detail["assignment_id"].as_string()).where(  # type: ignore[index]
            AuditLog.event_type == assignment_service.ASSIGNMENT_CLAIMED,
            AuditLog.detail["target_user_id"].as_string() == str(user_id),  # type: ignore[index]
        )
        about = (
            (AuditLog.detail["target_user_id"].as_string() == str(user_id))  # type: ignore[index]
            | ((AuditLog.event_type == CHANGE_MEMBERSHIP_LEAVE) & (AuditLog.user_id == str(user_id)))
            | AuditLog.detail["assignment_id"].as_string().in_(claimed)  # type: ignore[index]
        )
        query = query.where(about)
        count_query = count_query.where(about)
    total = session.exec(count_query).one()
    rows = session.exec(
        query.order_by(AuditLog.timestamp.desc(), AuditLog.id).offset((page - 1) * page_size).limit(page_size)  # type: ignore[attr-defined]
    ).all()

    user_ids = {u for row in rows for u in (_uuid(row.user_id), _uuid((row.detail or {}).get("target_user_id"))) if u}
    emails = (
        {u.id: u.email for u in session.exec(select(User).where(User.id.in_(user_ids))).all()}  # type: ignore[attr-defined]
        if user_ids
        else {}
    )
    codes = {
        j.id: j.code
        for j in session.exec(
            select(Jurisdiction).where(
                Jurisdiction.id.in_([u for u in (_uuid(row.active_jurisdiction_id) for row in rows) if u])  # type: ignore[attr-defined]
            )
        ).all()
    }
    items = []
    for row in rows:
        detail = row.detail or {}
        actor_id = _uuid(row.user_id)
        leaving = row.event_type == CHANGE_MEMBERSHIP_LEAVE
        target_id = row.user_id if leaving else detail.get("target_user_id")
        target_uuid = _uuid(target_id)
        roles = detail.get("roles")
        role = detail.get("role") or (roles[0] if isinstance(roles, list) and len(roles) == 1 else None)
        # A change made acting as SystemAdministrator (the row says so: ``audit_role_change``, or its
        # context) names neither the actor nor that context: the SystemAdministrator is invisible in the app.
        # (One acting in a stored role is stamped with the role and shown as its holder.)
        by_system = bool(detail.get("by_system")) or row.active_role == ROLE_SYSTEM_ADMIN
        items.append(
            HistoryEntry(
                id=row.id,
                timestamp=row.timestamp,
                actor_id=None if by_system else row.user_id,
                actor_email=SYSTEM_ACTOR_LABEL if by_system else (emails.get(actor_id, "") if actor_id else ""),
                actor_jurisdiction=None
                if by_system or not row.active_jurisdiction_id
                else codes.get(row.active_jurisdiction_id),
                actor_role=None if by_system else row.active_role,
                target_id=str(target_uuid) if target_uuid else None,
                target_email=(emails.get(target_uuid) if target_uuid else None) or detail.get("target_email") or "",
                change=row.event_type,
                jurisdiction=detail.get("jurisdiction") or detail.get("code"),
                role=role,
            )
        )
    return HistoryPage(items, total)

