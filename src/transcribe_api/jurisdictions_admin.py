"""Add jurisdictions and their members without a deployment (DIAAT-322).

The admin area does this through the API (DIAAT-356: /api/admin/jurisdictions,
routes_dictation_privileged.py), with the functions here. Locally, or where a pod
can be reached, the same from the command line:

    python -m transcribe_api.jurisdictions_admin list
    python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" < et.json
    python -m transcribe_api.jurisdictions_admin add-member ET judge@ejudiciary.net --role judge
    python -m transcribe_api.jurisdictions_admin remove-member ET judge@ejudiciary.net

``put`` creates or updates a jurisdiction; its settings (wording, template, features:
domain/jurisdictions.JurisdictionSettings) are validated first. ``add-member`` adds a
user who has signed in, or, for someone who has not, a pending membership they claim
on first sign-in (so an ET test judge is never put in the IAC). ``put`` reads the
settings from standard input only: the tool opens no file named on its command line.
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

from transcribe_api.domain.jurisdictions import ROLES, JurisdictionSettings
from transcribe_api.domain.models_dictation import Jurisdiction, JurisdictionMembership, User


class AdminError(Exception):
    """A refusal to show the operator, not a crash."""


class NotFoundError(AdminError):
    """No such jurisdiction, or no such member of it."""


class ConflictError(AdminError):
    """Changed by someone else meanwhile: load it again and retry."""


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
    auto_enrol: bool | None = None,
    enabled: bool | None = None,
    expected_version: str | None = None,
) -> str:
    """Create or update a jurisdiction. A flag left as None keeps its current value (a
    new jurisdiction: enabled, not auto-enrol), so updating the wording cannot turn
    the IAC's auto-enrol off or re-enable a disabled jurisdiction.

    ``expected_version``: the version the caller edited ("" for a new one). If the
    jurisdiction has changed since (or exists already), nothing is written."""
    code = code.strip().upper()
    if not code.isalnum() or len(code) > 16:
        msg = "The code must be letters and digits, at most 16"
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
        row = Jurisdiction(code=code, name=name, settings=validated, auto_enrol=False, enabled=True)
    row.name = name
    row.settings = validated
    if auto_enrol is not None:
        row.auto_enrol = auto_enrol
    if enabled is not None:
        row.enabled = enabled
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


def add_member(session: Session, code: str, email: str, role: str = "judge") -> str:
    """Add someone by sign-in email, or change their role. Someone who has not signed in
    yet is added pending, and joins when they first do."""
    if role not in ROLES:
        msg = f"The role must be one of {', '.join(ROLES)}"
        raise AdminError(msg)
    jurisdiction = _jurisdiction(session, code)
    email = email.strip().lower()
    users = _users_by_email(session, email)
    found: list[JurisdictionMembership | None]
    if users:
        found = [
            session.exec(
                select(JurisdictionMembership).where(
                    JurisdictionMembership.jurisdiction_id == jurisdiction.id, JurisdictionMembership.user_id == user.id
                )
            ).first()
            for user in users
        ]
        rows = [
            member or JurisdictionMembership(jurisdiction_id=jurisdiction.id, user_id=user.id)
            for member, user in zip(found, users, strict=True)
        ]
        what = "signed-in user"
    else:
        found = [
            session.exec(
                select(JurisdictionMembership).where(
                    JurisdictionMembership.jurisdiction_id == jurisdiction.id,
                    JurisdictionMembership.user_id.is_(None),
                    func.lower(JurisdictionMembership.email) == email,
                )
            ).first()
        ]
        rows = [found[0] or JurisdictionMembership(jurisdiction_id=jurisdiction.id, email=email)]
        what = "pending until they sign in"
    existed = all(member is not None for member in found)
    for row in rows:
        row.role = role
        session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        msg = f"{email} was added to {jurisdiction.code} at the same moment; load the members again"
        raise ConflictError(msg) from None
    return f"{'Updated' if existed else 'Added'} {role} in {jurisdiction.code} ({what})"


def remove_member(session: Session, code: str, email: str) -> str:
    """Remove someone, signed in or pending, by sign-in email."""
    jurisdiction = _jurisdiction(session, code)
    email = email.strip().lower()
    users = _users_by_email(session, email)
    who = JurisdictionMembership.user_id.is_(None) & (func.lower(JurisdictionMembership.email) == email)
    if users:
        who = who | JurisdictionMembership.user_id.in_([user.id for user in users])
    rows = session.exec(
        select(JurisdictionMembership).where(JurisdictionMembership.jurisdiction_id == jurisdiction.id, who)
    ).all()
    if not rows:
        msg = f"Not a member of {jurisdiction.code}"
        raise NotFoundError(msg)
    for row in rows:
        session.delete(row)
    for user in users:
        if user.active_jurisdiction_id == jurisdiction.id:
            user.active_jurisdiction_id = None
            session.add(user)
    session.commit()
    return f"Removed from {jurisdiction.code}"


def jurisdictions_overview(session: Session) -> list[dict]:
    """Every jurisdiction, with its settings and how many members it has."""
    counts = dict(
        session.exec(
            select(JurisdictionMembership.jurisdiction_id, func.count(JurisdictionMembership.id)).group_by(
                JurisdictionMembership.jurisdiction_id
            )
        ).all()
    )
    return [
        {
            "code": row.code,
            "name": row.name,
            "enabled": row.enabled,
            "auto_enrol": row.auto_enrol,
            "settings": row.settings,
            "member_count": counts.get(row.id, 0),
            "version": version(row),
        }
        for row in session.exec(select(Jurisdiction).order_by(Jurisdiction.name)).all()
    ]


def members_of(session: Session, code: str) -> list[dict]:
    """A jurisdiction's members: who (their sign-in email), their role, and whether they
    have yet to sign in (pending)."""
    jurisdiction = _jurisdiction(session, code)
    rows = session.exec(
        select(JurisdictionMembership, User.email)
        .join(User, User.id == JurisdictionMembership.user_id, isouter=True)
        .where(JurisdictionMembership.jurisdiction_id == jurisdiction.id)
    ).all()
    # One entry per person: a pending row left beside their membership, or two user
    # rows with one email, is still one member (signed in, if any row is).
    members: dict[str, dict] = {}
    for membership, user_email in rows:
        email = (user_email or membership.email or "").lower()
        member = {"email": email, "role": membership.role, "pending": user_email is None}
        if email not in members or (members[email]["pending"] and not member["pending"]):
            members[email] = member
    return sorted(members.values(), key=lambda m: m["email"])


def list_jurisdictions(session: Session) -> str:
    lines = []
    for row in session.exec(select(Jurisdiction).order_by(Jurisdiction.code)).all():
        members = session.exec(
            select(func.count(JurisdictionMembership.id)).where(JurisdictionMembership.jurisdiction_id == row.id)
        ).one()
        flags = [f for f, on in (("enabled", row.enabled), ("auto-enrol", row.auto_enrol)) if on]
        lines.append(f"{row.code}\t{row.name}\t{members} member(s)\t{', '.join(flags) or 'disabled'}")
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
        "--auto-enrol",
        action=argparse.BooleanOptionalAction,
        help="new users with nothing pending join it (unchanged if not given; new: off)",
    )
    put.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction, help="(unchanged if not given; new: on)"
    )
    for name in ("add-member", "remove-member"):
        member = commands.add_parser(name)
        member.add_argument("code")
        member.add_argument("email")
        if name == "add-member":
            member.add_argument("--role", default="judge", choices=ROLES)
    args = parser.parse_args(argv)

    if args.command == "list":
        return list_jurisdictions(session)
    if args.command == "put":
        return put_jurisdiction(
            session,
            args.code,
            args.name,
            _read_settings(),
            auto_enrol=args.auto_enrol,
            enabled=args.enabled,
        )
    if args.command == "add-member":
        return add_member(session, args.code, args.email, args.role)
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
