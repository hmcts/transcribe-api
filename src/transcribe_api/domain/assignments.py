"""Role assignments (DIAAT-382): who holds which role in which jurisdiction.

The ``role_assignment`` table is the source of truth for what a user may be. A row is
(user, jurisdiction, role), or, for someone added by email before their first sign-in,
(pending email, jurisdiction, role): claimed when they first sign in. Judge,
LegalTextManager and JurisdictionAdministrator are held in a jurisdiction; the
JurisdictionSuperAdministrator is global (no jurisdiction). SystemAdministrator is never
stored (it comes only from the Entra token), and neither is Normal.

These are the building blocks the sign-in (domain/auth/sessions.py), the user's own
actions (leave a jurisdiction) and, later, the administration endpoints use. Nothing here
decides who may call them: that is the caller's (the permission map and the assignment
rules, DIAAT-383). Every function takes the caller's session and leaves the commit to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import delete
from sqlmodel import Session, select

from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.permissions import GLOBAL_ROLES, STORED_ROLES
from transcribe_api.domain.jurisdiction_stages import sees
from transcribe_api.domain.models_dictation import Jurisdiction, RoleAssignment

logger = logging.getLogger(__name__)


class AssignmentError(ValueError):
    """An assignment the rules refuse (a role that cannot be stored, a jurisdiction where
    there must be none)."""


class DuplicateAssignmentError(AssignmentError):
    """The holder already has that role there."""


@dataclass(frozen=True)
class Assignment:
    """One assignment, with its jurisdiction (None: global)."""

    id: UUID
    role: str
    jurisdiction_id: UUID | None
    jurisdiction_code: str | None
    jurisdiction_name: str | None

    @property
    def context(self) -> Context:
        return Context(jurisdiction=self.jurisdiction_code, role=self.role)


def normalise_email(email: str | None) -> str:
    """The form a pending email is stored and matched in: trimmed, lower case."""
    return (email or "").strip().lower()


def _sort_key(assignment: Assignment) -> tuple[str, int]:
    return (assignment.jurisdiction_name or "", STORED_ROLES.index(assignment.role))


def load_assignments(session: Session, user_id: UUID) -> list[Assignment]:
    """The user's assignments, by jurisdiction name then role.

    An assignment in a disabled jurisdiction is left out, as if the user had none there
    (a disabled jurisdiction is invisible to its members), and so is one in a jurisdiction
    whose stage does not let them see it (DIAAT-345: a draft only its administrators, one in
    test its testers and administrators). Global ones are always there.
    """
    rows = session.exec(
        select(RoleAssignment, Jurisdiction)
        .join(Jurisdiction, Jurisdiction.id == RoleAssignment.jurisdiction_id, isouter=True)
        .where(RoleAssignment.user_id == user_id)
    ).all()
    assignments = [
        Assignment(
            id=row.id,
            role=row.role,
            jurisdiction_id=row.jurisdiction_id,
            jurisdiction_code=jurisdiction.code if jurisdiction else None,
            jurisdiction_name=jurisdiction.name if jurisdiction else None,
        )
        for row, jurisdiction in rows
        if row.role in STORED_ROLES
        and (jurisdiction is None or (jurisdiction.enabled and sees(jurisdiction.stage, row.role, row.tester)))
    ]
    return sorted(assignments, key=_sort_key)


def distinct_contexts(assignments: list[Assignment]) -> list[Context]:
    """The (jurisdiction, role) pairs, once each, in the order of the assignments."""
    seen: dict[Context, None] = {}
    for assignment in assignments:
        seen.setdefault(assignment.context, None)
    return list(seen)


def check_assignable(role: str, jurisdiction_id: UUID | None) -> None:
    """The rules the database also enforces, as a refusal to show: a stored role, held
    in a jurisdiction unless it is the global one."""
    if role not in STORED_ROLES:
        msg = f"{role!r} cannot be assigned"
        raise AssignmentError(msg)
    if role in GLOBAL_ROLES and jurisdiction_id is not None:
        msg = f"{role} is global: it has no jurisdiction"
        raise AssignmentError(msg)
    if role not in GLOBAL_ROLES and jurisdiction_id is None:
        msg = f"{role} needs a jurisdiction"
        raise AssignmentError(msg)


def find_assignment(
    session: Session, *, role: str, jurisdiction_id: UUID | None, user_id: UUID | None = None, email: str | None = None
) -> RoleAssignment | None:
    query = select(RoleAssignment).where(RoleAssignment.role == role)
    query = query.where(
        RoleAssignment.jurisdiction_id.is_(None)
        if jurisdiction_id is None
        else RoleAssignment.jurisdiction_id == jurisdiction_id
    )
    if user_id is not None:
        query = query.where(RoleAssignment.user_id == user_id)
    else:
        query = query.where(RoleAssignment.user_id.is_(None), RoleAssignment.pending_email == normalise_email(email))
    return session.exec(query).first()


def add_assignment(
    session: Session,
    *,
    role: str,
    jurisdiction_id: UUID | None,
    user_id: UUID | None = None,
    email: str | None = None,
    granted_by: UUID | None = None,
    tester: bool = False,
) -> RoleAssignment:
    """Give a signed-in user (``user_id``), or someone who has not signed in yet
    (``email``: a pending assignment, claimed at their first sign-in), a role. ``tester``: in
    the jurisdiction's test group (DIAAT-345).

    Raises AssignmentError for what the rules refuse, and DuplicateAssignmentError when
    they already hold it there. The row is flushed, not committed."""
    check_assignable(role, jurisdiction_id)
    pending_email = normalise_email(email)
    if (user_id is None) == (not pending_email):
        msg = "name the user or an email, not both and not neither"
        raise AssignmentError(msg)
    if find_assignment(session, role=role, jurisdiction_id=jurisdiction_id, user_id=user_id, email=pending_email):
        msg = f"already holds {role} there"
        raise DuplicateAssignmentError(msg)
    row = RoleAssignment(
        user_id=user_id,
        pending_email=None if user_id is not None else pending_email,
        jurisdiction_id=jurisdiction_id,
        role=role,
        granted_by=granted_by,
        tester=tester,
    )
    session.add(row)
    session.flush()
    return row


def ensure_assignment(session: Session, **kwargs) -> tuple[RoleAssignment, bool]:
    """``add_assignment``, or the existing row: (row, created). For scripts that may run twice."""
    try:
        return add_assignment(session, **kwargs), True
    except DuplicateAssignmentError:
        return find_assignment(session, **{k: v for k, v in kwargs.items() if k != "granted_by"}), False


def remove_assignment(session: Session, assignment_id: UUID) -> bool:
    """Delete one assignment. False if there is none."""
    row = session.get(RoleAssignment, assignment_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def claim_pending_assignments(session: Session, user_id: UUID, email: str | None) -> int:
    """Give a user who has just signed in for the first time what was assigned to their
    email beforehand. Case-insensitive (the email is matched in the lower case it is
    stored in). A pending row for something the user already holds is dropped, not
    claimed twice. Returns how many were claimed. Flushed, not committed."""
    pending_email = normalise_email(email)
    if not pending_email:
        return 0
    pending = session.exec(
        select(RoleAssignment).where(RoleAssignment.user_id.is_(None), RoleAssignment.pending_email == pending_email)
    ).all()
    claimed = 0
    for row in pending:
        held = find_assignment(session, role=row.role, jurisdiction_id=row.jurisdiction_id, user_id=user_id)
        if held is not None:
            session.delete(row)
        else:
            row.user_id = user_id
            row.pending_email = None
            session.add(row)
            claimed += 1
        session.flush()
    if pending:
        logger.info("Claimed %d pending assignment(s) for user id=%s", claimed, user_id)
    return claimed


class Left(NamedTuple):
    """A user leaving a jurisdiction: its ID (None: there is none by that code) and the
    roles they held there, sorted (empty: they held none)."""

    jurisdiction_id: UUID | None
    roles: list[str]


def leave_jurisdiction(session: Session, user_id: UUID, jurisdiction_code: str) -> Left:
    """Remove all the user's assignments in a jurisdiction (the user leaving it): theirs
    only, and only there (a global role and other jurisdictions are untouched). Flushed,
    not committed."""
    jurisdiction = session.exec(
        select(Jurisdiction).where(Jurisdiction.code == jurisdiction_code.strip().upper())
    ).first()
    if jurisdiction is None:
        return Left(None, [])
    roles = sorted(
        session.exec(
            select(RoleAssignment.role).where(
                RoleAssignment.user_id == user_id, RoleAssignment.jurisdiction_id == jurisdiction.id
            )
        ).all()
    )
    if roles:
        session.execute(
            delete(RoleAssignment).where(
                RoleAssignment.user_id == user_id, RoleAssignment.jurisdiction_id == jurisdiction.id
            )
        )
        session.flush()
    return Left(jurisdiction.id, roles)
