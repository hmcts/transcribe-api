"""Add jurisdictions and their members without a deployment (DIAAT-322, "manual first").

Until the admin area does this (DIAAT-328), the product team runs it in an API pod:

    python -m transcribe_api.jurisdictions_admin list
    python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" --settings et.json
    python -m transcribe_api.jurisdictions_admin add-member ET judge@ejudiciary.net --role judge
    python -m transcribe_api.jurisdictions_admin remove-member ET judge@ejudiciary.net

``put`` creates or updates a jurisdiction; its settings (wording, template, features:
domain/jurisdictions.JurisdictionSettings) are validated first. ``add-member`` adds a
user who has signed in, or, for someone who has not, a pending membership they claim
on first sign-in (so an ET test judge is never put in the IAC). Settings can be read
from standard input with ``--settings -``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func
from sqlmodel import Session, select

from transcribe_api.domain.jurisdictions import ROLES, JurisdictionSettings
from transcribe_api.domain.models_dictation import Jurisdiction, JurisdictionMembership, User


class AdminError(Exception):
    """A refusal to show the operator, not a crash."""


def _jurisdiction(session: Session, code: str) -> Jurisdiction:
    row = session.exec(select(Jurisdiction).where(Jurisdiction.code == code.upper())).first()
    if row is None:
        msg = f"No jurisdiction {code.upper()}"
        raise AdminError(msg)
    return row


def _user_by_email(session: Session, email: str) -> User | None:
    users = session.exec(select(User).where(func.lower(User.email) == email)).all()
    if len(users) > 1:
        msg = f"{len(users)} users have that email; add them in the database"
        raise AdminError(msg)
    return users[0] if users else None


def put_jurisdiction(
    session: Session,
    code: str,
    name: str,
    settings: dict,
    *,
    auto_enrol: bool | None = None,
    enabled: bool | None = None,
) -> str:
    """Create or update a jurisdiction. A flag left as None keeps its current value (a
    new jurisdiction: enabled, not auto-enrol), so updating the wording cannot turn
    the IAC's auto-enrol off or re-enable a disabled jurisdiction."""
    code = code.strip().upper()
    if not code.isalnum() or len(code) > 16:
        msg = "The code must be letters and digits, at most 16"
        raise AdminError(msg)
    try:
        validated = JurisdictionSettings.model_validate(settings).model_dump(mode="json")
    except ValueError as exc:
        msg = f"Invalid settings: {exc}"
        raise AdminError(msg) from None
    row = session.exec(select(Jurisdiction).where(Jurisdiction.code == code)).first()
    created = row is None
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
    session.commit()
    return f"{'Created' if created else 'Updated'} {code}"


def add_member(session: Session, code: str, email: str, role: str = "judge") -> str:
    if role not in ROLES:
        msg = f"The role must be one of {', '.join(ROLES)}"
        raise AdminError(msg)
    jurisdiction = _jurisdiction(session, code)
    email = email.strip().lower()
    user = _user_by_email(session, email)
    if user is not None:
        existing = session.exec(
            select(JurisdictionMembership).where(
                JurisdictionMembership.jurisdiction_id == jurisdiction.id, JurisdictionMembership.user_id == user.id
            )
        ).first()
        member = existing or JurisdictionMembership(jurisdiction_id=jurisdiction.id, user_id=user.id)
        what = "signed-in user"
    else:
        existing = session.exec(
            select(JurisdictionMembership).where(
                JurisdictionMembership.jurisdiction_id == jurisdiction.id,
                JurisdictionMembership.user_id.is_(None),
                JurisdictionMembership.email == email,
            )
        ).first()
        member = existing or JurisdictionMembership(jurisdiction_id=jurisdiction.id, email=email)
        what = "pending until they sign in"
    member.role = role
    session.add(member)
    session.commit()
    return f"{'Updated' if existing else 'Added'} {role} in {jurisdiction.code} ({what})"


def remove_member(session: Session, code: str, email: str) -> str:
    jurisdiction = _jurisdiction(session, code)
    email = email.strip().lower()
    user = _user_by_email(session, email)
    who = (
        JurisdictionMembership.user_id == user.id
        if user is not None
        else (JurisdictionMembership.user_id.is_(None) & (JurisdictionMembership.email == email))
    )
    rows = session.exec(
        select(JurisdictionMembership).where(JurisdictionMembership.jurisdiction_id == jurisdiction.id, who)
    ).all()
    if not rows:
        msg = f"Not a member of {jurisdiction.code}"
        raise AdminError(msg)
    for row in rows:
        session.delete(row)
    if user is not None and user.active_jurisdiction_id == jurisdiction.id:
        user.active_jurisdiction_id = None
        session.add(user)
    session.commit()
    return f"Removed from {jurisdiction.code}"


def list_jurisdictions(session: Session) -> str:
    lines = []
    for row in session.exec(select(Jurisdiction).order_by(Jurisdiction.code)).all():
        members = session.exec(
            select(func.count(JurisdictionMembership.id)).where(JurisdictionMembership.jurisdiction_id == row.id)
        ).one()
        flags = [f for f, on in (("enabled", row.enabled), ("auto-enrol", row.auto_enrol)) if on]
        lines.append(f"{row.code}\t{row.name}\t{members} member(s)\t{', '.join(flags) or 'disabled'}")
    return "\n".join(lines) or "No jurisdictions"


def _read_settings(path: str) -> dict:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"The settings are not JSON: {exc}"
        raise AdminError(msg) from None


def run(argv: list[str], session: Session) -> str:
    parser = argparse.ArgumentParser(prog="python -m transcribe_api.jurisdictions_admin", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    put = commands.add_parser("put", help="create or update a jurisdiction")
    put.add_argument("code")
    put.add_argument("--name", required=True)
    put.add_argument("--settings", required=True, help="a JSON file, or - for standard input")
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
            _read_settings(args.settings),
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
