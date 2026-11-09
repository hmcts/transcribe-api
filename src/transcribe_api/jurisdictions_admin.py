"""Add jurisdictions and their members without a deployment (DIAAT-322).

The admin area does this through the API (DIAAT-356: /api/admin/jurisdictions,
routes_dictation_privileged.py), with the functions here. Locally, or where a pod
can be reached, the same from the command line:

    python -m transcribe_api.jurisdictions_admin list
    python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" < et.json
    python -m transcribe_api.jurisdictions_admin add-member ET admin@ejudiciary.net --role admin
    python -m transcribe_api.jurisdictions_admin add-member ET judge@ejudiciary.net --role judge --tester
    python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" --stage test < et.json
    python -m transcribe_api.jurisdictions_admin remove-member ET judge@ejudiciary.net

``put`` creates or updates a jurisdiction; its settings (wording, template, features:
domain/jurisdictions.JurisdictionSettings) are validated first. ``add-member`` gives a user
who has signed in a role in the jurisdiction (``judge``: Judge, ``admin``:
JurisdictionAdministrator), or, for someone who has not, a pending assignment they claim on
first sign-in (domain/assignments.py). A member here is a ``role_assignment`` row (DIAAT-382):
nothing is enrolled automatically any more (DIAAT-373). ``put`` reads the settings from
standard input only: the tool opens no file named on its command line.

DIAAT-345: a new jurisdiction is a draft, seen only by its admins. Moved to ``test``, its
testers (``--tester``) see it too; ``live``, every member. It leaves draft only when it is
ready (domain/jurisdictions.readiness_problems).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from transcribe_api.domain.assignments import (
    DuplicateAssignmentError,
    add_assignment,
    find_assignment,
)
from transcribe_api.domain.auth.permissions import ROLE_JUDGE as ASSIGNED_JUDGE
from transcribe_api.domain.auth.permissions import ROLE_JURISDICTION_ADMIN as ASSIGNED_ADMIN
from transcribe_api.domain.jurisdictions import (
    ROLE_ADMIN,
    ROLE_JUDGE,
    ROLES,
    STAGE_DRAFT,
    STAGES,
    JurisdictionSettings,
    parse_settings,
    readiness_problems,
)
from transcribe_api.domain.models_dictation import Jurisdiction, RoleAssignment, User

# The member roles of this tool, and the assignments they stand for.
_ASSIGNED_ROLE = {ROLE_JUDGE: ASSIGNED_JUDGE, ROLE_ADMIN: ASSIGNED_ADMIN}
_MEMBER_ROLE = {assigned: member for member, assigned in _ASSIGNED_ROLE.items()}
# Either role is a "member" role; the others (LegalTextManager, ...) are assigned by the
# administration endpoints, and shown here as their own name.


class AdminError(Exception):
    """A refusal to show the operator, not a crash."""


class NotFoundError(AdminError):
    """No such jurisdiction, or no such member of it."""


class ConflictError(AdminError):
    """Changed by someone else meanwhile: load it again and retry."""


class NotReadyError(AdminError):
    """Not ready to be switched on (DIAAT-345); ``problems`` says what is missing."""

    def __init__(self, code: str, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"{code} is not ready to be switched on. {' '.join(problems)}")


def _admin_counts(session: Session) -> dict:
    """Its administrators, signed in or pending, by jurisdiction."""
    return dict(
        session.exec(
            select(RoleAssignment.jurisdiction_id, func.count(RoleAssignment.id))
            .where(RoleAssignment.role == ASSIGNED_ADMIN)
            .group_by(RoleAssignment.jurisdiction_id)
        ).all()
    )


def _admin_count(session: Session, jurisdiction_id) -> int:
    return _admin_counts(session).get(jurisdiction_id, 0)


def _optional_text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _jurisdiction(session: Session, code: str, *, lock: bool = False) -> Jurisdiction:
    """``lock``: locked until the commit, and read afresh once the lock is held, not
    taken from what this session read before (which may predate a change)."""
    code = code.strip().upper()
    query = select(Jurisdiction).where(Jurisdiction.code == code)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = session.exec(query).first()
    if row is None:
        msg = f"No jurisdiction {code}"
        raise NotFoundError(msg)
    return row


def _users_by_email(session: Session, email: str) -> list[User]:
    """Every user row with that sign-in email. Usually one; an Entra account that was
    deleted and recreated leaves two, and the same person is meant either way."""
    return list(session.exec(select(User).where(func.lower(User.email) == email)).all())


def user_ids_for(session: Session, email: str) -> list[str]:
    """The user IDs with that sign-in email (none until they first sign in)."""
    return sorted(str(user.id) for user in _users_by_email(session, email.strip().lower()))


def wording_and_version(session: Session, code: str) -> tuple[dict[str, str], str]:
    """A jurisdiction's stored wording and its version, from the one read: a version
    newer than the wording shown would let a save undo the change between them."""
    row = _jurisdiction(session, code)
    return dict((row.settings or {}).get("wording") or {}), version(row)


def version(row: Jurisdiction) -> str:
    """When ``row`` last changed, as the admin area's editor saw it: a save naming an
    older version is refused (ConflictError) instead of undoing someone's change."""
    stamp = row.updated_datetime
    if stamp is None:
        return ""
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(UTC).replace(tzinfo=None)
    return stamp.isoformat()


def put_jurisdiction(
    session: Session,
    code: str,
    name: str,
    settings: dict,
    *,
    enabled: bool | None = None,
    expected_version: str | None = None,
    stage: str | None = None,
    display_name: str | None = None,
    service: str | None = None,
) -> str:
    """Create or update a jurisdiction. ``enabled`` left as None keeps its current value (a
    new jurisdiction: enabled, a draft), so updating the wording cannot re-enable a disabled
    jurisdiction. So are the stage, the display name and the service (blank: none).

    It leaves draft only when it is ready (NotReadyError): its required screen wording set,
    and an administrator assigned.

    ``expected_version``: the version the caller edited ("" for a new one). If the
    jurisdiction has changed since (or exists already), nothing is written."""
    code = code.strip().upper()
    if not code.isalnum() or len(code) > 16:
        msg = "The code must be letters and digits, at most 16"
        raise AdminError(msg)
    if stage is not None and stage not in STAGES:
        msg = f"The stage must be one of {', '.join(STAGES)}"
        raise AdminError(msg)
    try:
        validated = JurisdictionSettings.model_validate(settings).model_dump(mode="json")
    except ValueError as exc:
        msg = f"Invalid settings: {exc}"
        raise AdminError(msg) from None
    # Locked until the commit, so two saves of the same version cannot both pass.
    row = session.exec(
        select(Jurisdiction)
        .where(Jurisdiction.code == code)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    created = row is None
    if expected_version is not None and (version(row) if row is not None else "") != expected_version:
        msg = (
            f"{code} already exists"
            if not expected_version
            else f"{code} was changed by someone else meanwhile; load it again"
        )
        raise ConflictError(msg)
    if created:
        row = Jurisdiction(code=code, name=name, settings=validated, enabled=True, stage=STAGE_DRAFT)
    if stage is not None and stage != STAGE_DRAFT and row.stage == STAGE_DRAFT:
        admins = 0 if created else _admin_count(session, row.id)
        problems = readiness_problems(JurisdictionSettings.model_validate(validated), admin_count=admins)
        if problems:
            session.rollback()  # the lock
            raise NotReadyError(code, problems)
    row.name = name
    if display_name is not None:
        row.display_name = _optional_text(display_name)
    if service is not None:
        row.service = _optional_text(service)
    row.settings = validated
    if enabled is not None:
        row.enabled = enabled
    if stage is not None:
        row.stage = stage
    row.updated_datetime = datetime.now(UTC)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:  # created by someone else at the same moment
        session.rollback()
        msg = f"{code} already exists"
        raise ConflictError(msg) from None
    return f"{'Created' if created else 'Updated'} {code}"


class WordingSaved(NamedTuple):
    """A wording save, all read under its lock: the wording it replaced, the wording
    stored, and the version that names it (a later read could name someone else's)."""

    before: dict[str, str]
    wording: dict[str, str]
    version: str


def set_wording(
    session: Session, code: str, wording: dict[str, str], *, expected_version: str | None = None
) -> WordingSaved:
    """Replace a jurisdiction's wording only (DIAAT-356: what its own admins change).
    Every key sent is checked, blank or not; blank text means the default. The rest
    of the stored settings is kept as stored, including anything this release does
    not know (a newer release's template, during a rolling deploy). A stale
    ``expected_version`` is refused (ConflictError), as for put_jurisdiction."""
    row = _jurisdiction(session, code, lock=True)
    if expected_version is not None and version(row) != expected_version:
        msg = f"{row.code}'s wording was changed by someone else meanwhile; load it again"
        raise ConflictError(msg)
    trimmed = {key: text.strip() for key, text in wording.items()}
    try:
        JurisdictionSettings(wording=trimmed)
    except ValueError as exc:
        msg = f"Invalid wording: {exc}"
        raise AdminError(msg) from None
    kept = {key: text for key, text in trimmed.items() if text}
    before = dict((row.settings or {}).get("wording") or {})
    row.settings = {**(row.settings or {}), "wording": kept}
    row.updated_datetime = datetime.now(UTC)
    session.add(row)
    session.flush()
    session.refresh(row)  # the version as stored, still under the lock
    saved = WordingSaved(before, kept, version(row))
    session.commit()
    return saved


def add_member(session: Session, code: str, email: str, role: str = "judge", *, tester: bool | None = None) -> str:
    """Add someone by sign-in email, or change their role. Someone who has not signed in
    yet is added pending, and joins when they first do. The member roles are ``judge`` and
    ``admin`` (Judge and JurisdictionAdministrator): the other roles are not given here.
    ``tester``: in the test group (DIAAT-345); None keeps it (a new member: not)."""
    if role not in ROLES:
        msg = f"The role must be one of {', '.join(ROLES)}"
        raise AdminError(msg)
    jurisdiction = _jurisdiction(session, code)
    email = email.strip().lower()
    users = _users_by_email(session, email)
    assigned = _ASSIGNED_ROLE[role]
    others = [r for r in _MEMBER_ROLE if r != assigned]
    existed = False
    if users:
        holders = [{"user_id": user.id} for user in users]
        what = "signed-in user"
    else:
        holders = [{"email": email}]
        what = "pending until they sign in"
    for holder in holders:
        # Changing a role: the other member role they held in this jurisdiction goes.
        for other in others:
            row = find_assignment(session, role=other, jurisdiction_id=jurisdiction.id, **holder)
            if row is not None:
                session.delete(row)
                existed = True
        try:
            add_assignment(
                session, role=assigned, jurisdiction_id=jurisdiction.id, tester=bool(tester), **holder
            )
        except DuplicateAssignmentError:
            existed = True
        if tester is not None:
            # The test group is of the person in the jurisdiction: every row of theirs there.
            for row in _holder_rows(session, jurisdiction.id, holder):
                row.tester = tester
                session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        msg = f"{email} was added to {jurisdiction.code} at the same moment; load the members again"
        raise ConflictError(msg) from None
    return f"{'Updated' if existed else 'Added'} {role} in {jurisdiction.code} ({what})"


def _holder_rows(session: Session, jurisdiction_id, holder: dict) -> list[RoleAssignment]:
    """A holder's assignments in a jurisdiction (``holder``: ``user_id`` or ``email``)."""
    query = select(RoleAssignment).where(RoleAssignment.jurisdiction_id == jurisdiction_id)
    if "user_id" in holder:
        query = query.where(RoleAssignment.user_id == holder["user_id"])
    else:
        query = query.where(RoleAssignment.user_id.is_(None), RoleAssignment.pending_email == holder["email"])
    return list(session.exec(query).all())


def remove_member(session: Session, code: str, email: str) -> str:
    """Remove someone, signed in or pending, by sign-in email: all their roles there."""
    jurisdiction = _jurisdiction(session, code)
    email = email.strip().lower()
    users = _users_by_email(session, email)
    who = RoleAssignment.user_id.is_(None) & (RoleAssignment.pending_email == email)
    if users:
        who = who | RoleAssignment.user_id.in_([user.id for user in users])
    rows = session.exec(select(RoleAssignment).where(RoleAssignment.jurisdiction_id == jurisdiction.id, who)).all()
    if not rows:
        msg = f"Not a member of {jurisdiction.code}"
        raise NotFoundError(msg)
    for row in rows:
        session.delete(row)
    session.commit()
    return f"Removed from {jurisdiction.code}"


def _member_counts(session: Session) -> dict:
    """Members by jurisdiction: people, not roles (someone who is both is one member)."""
    members: dict = {}
    for jurisdiction_id, user_id, pending_email in session.exec(
        select(RoleAssignment.jurisdiction_id, RoleAssignment.user_id, RoleAssignment.pending_email).where(
            RoleAssignment.jurisdiction_id.is_not(None)
        )
    ).all():
        members.setdefault(jurisdiction_id, set()).add(user_id or pending_email)
    return {jurisdiction_id: len(people) for jurisdiction_id, people in members.items()}


def jurisdictions_overview(session: Session) -> list[dict]:
    """Every jurisdiction, with its settings, how many members it has, and what stops it
    being switched on (``problems``, DIAAT-345)."""
    counts = _member_counts(session)
    admins = _admin_counts(session)
    return [
        {
            "code": row.code,
            "name": row.name,
            "display_name": row.display_name,
            "service": row.service,
            "stage": row.stage,
            "enabled": row.enabled,
            "settings": row.settings,
            "member_count": counts.get(row.id, 0),
            "problems": readiness_problems(parse_settings(row.settings, row.code), admin_count=admins.get(row.id, 0)),
            "version": version(row),
        }
        for row in session.exec(select(Jurisdiction).order_by(Jurisdiction.name)).all()
    ]


def members_of(session: Session, code: str) -> list[dict]:
    """A jurisdiction's members: who (their sign-in email), their role, whether they are in
    its test group, and whether they have yet to sign in (pending). The role is ``admin`` or
    ``judge`` for those, else the assigned role's name."""
    jurisdiction = _jurisdiction(session, code)
    rows = session.exec(
        select(RoleAssignment, User.email)
        .join(User, User.id == RoleAssignment.user_id, isouter=True)
        .where(RoleAssignment.jurisdiction_id == jurisdiction.id)
    ).all()
    # One entry per person: a pending row left beside their assignment, or two user
    # rows with one email, is still one member (signed in, if any row is); and the
    # highest role they hold there is the one shown.
    members: dict[str, dict] = {}
    for assignment, user_email in rows:
        email = (user_email or assignment.pending_email or "").lower()
        member = {
            "email": email,
            "role": _MEMBER_ROLE.get(assignment.role, assignment.role),
            "tester": assignment.tester,
            "pending": user_email is None,
        }
        current = members.get(email)
        if current is None:
            members[email] = member
            continue
        if current["pending"] and not member["pending"]:
            current["pending"] = False
        current["tester"] = current["tester"] or member["tester"]
        if member["role"] == ROLE_ADMIN:
            current["role"] = ROLE_ADMIN
    return sorted(members.values(), key=lambda m: m["email"])


def list_jurisdictions(session: Session) -> str:
    counts = _member_counts(session)
    lines = []
    for row in session.exec(select(Jurisdiction).order_by(Jurisdiction.code)).all():
        lines.append(
            f"{row.code}\t{row.name}\t{row.stage}\t{counts.get(row.id, 0)} member(s)\t"
            f"{'enabled' if row.enabled else 'disabled'}"
        )
    return "\n".join(lines) or "No jurisdictions"


def _read_settings() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        msg = f"The settings are not JSON: {exc}"
        raise AdminError(msg) from None


def run(argv: list[str], session: Session) -> str:
    parser = argparse.ArgumentParser(prog="python -m transcribe_api.jurisdictions_admin", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    put = commands.add_parser("put", help="create or update a jurisdiction; its settings (JSON) on standard input")
    put.add_argument("code")
    put.add_argument("--name", required=True)
    put.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction, help="(unchanged if not given; new: on)"
    )
    put.add_argument("--stage", choices=STAGES, help="who sees it (unchanged if not given; new: draft)")
    put.add_argument("--display-name", help="what users see (unchanged if not given; blank: the name)")
    put.add_argument("--service", help="the service it is for (unchanged if not given; blank: none)")
    for name in ("add-member", "remove-member"):
        member = commands.add_parser(name)
        member.add_argument("code")
        member.add_argument("email")
        if name == "add-member":
            member.add_argument("--role", default="judge", choices=ROLES)
            member.add_argument(
                "--tester", action=argparse.BooleanOptionalAction, help="in the test group (unchanged if not given)"
            )
    args = parser.parse_args(argv)

    if args.command == "list":
        return list_jurisdictions(session)
    if args.command == "put":
        return put_jurisdiction(
            session,
            args.code,
            args.name,
            _read_settings(),
            enabled=args.enabled,
            stage=args.stage,
            display_name=args.display_name,
            service=args.service,
        )
    if args.command == "add-member":
        return add_member(session, args.code, args.email, args.role, tester=args.tester)
    return remove_member(session, args.code, args.email)


def main(argv: list[str] | None = None) -> int:
    from transcribe_api.runtime.db_connection import get_engine

    try:
        with Session(get_engine()) as session:
            print(run(sys.argv[1:] if argv is None else argv, session))
    except AdminError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
