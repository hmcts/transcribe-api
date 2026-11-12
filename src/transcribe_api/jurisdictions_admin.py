"""Add jurisdictions without a deployment (DIAAT-322).

The admin area does this through the API (DIAAT-356: /api/admin/jurisdictions,
routes_dictation_privileged.py), with the functions here. Locally, or where a pod
can be reached, the same from the command line:

    python -m transcribe_api.jurisdictions_admin list
    python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" < et.json
    python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" --stage live \
        --signed-off-by "Service lead's name" < et.json

``put`` creates or updates a jurisdiction; its settings (wording, template, features:
domain/jurisdictions.JurisdictionSettings) are validated first. It reads the settings from
standard input only: the tool opens no file named on its command line. Who holds a role in a
jurisdiction is a ``role_assignment`` row (DIAAT-382), given and taken away through the
assignment endpoints (api/routes_admin_users.py, DIAAT-383); the command line no longer does it.

DIAAT-347: a jurisdiction is switched on for everyone (``live``) only with the service lead's
sign-off recorded (``--signed-off-by``): set_stage, which templates use too.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from transcribe_api.domain.auth.permissions import ROLE_JURISDICTION_ADMIN as ASSIGNED_ADMIN
from transcribe_api.domain.jurisdictions import (
    STAGE_DRAFT,
    STAGE_LIVE,
    STAGES,
    JurisdictionSettings,
    parse_settings,
    readiness_problems,
)
from transcribe_api.domain.models_dictation import Jurisdiction, JurisdictionTemplate, RoleAssignment


class AdminError(Exception):
    """A refusal to show the operator, not a crash."""


class NotFoundError(AdminError):
    """No such jurisdiction."""


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
class NotSignedOffError(AdminError):
    """Switched on for everyone without the service lead's sign-off (DIAAT-347)."""

    def __init__(self) -> None:
        super().__init__(
            "Record the service lead's sign-off before switching it on for everyone: "
            "their name, and that they have signed it off."
        )


MAX_SIGN_OFF_NAME = 200


class SignOff(NamedTuple):
    """The service lead's sign-off for going live (DIAAT-347): who signed it off, and
    the admin who recorded it (None from the command line)."""

    service_lead: str
    recorded_by: UUID | None = None


def set_stage(row: Jurisdiction | JurisdictionTemplate, stage: str, sign_off: SignOff | None) -> None:
    """Move a jurisdiction or template to ``stage``. Going live needs ``sign_off``
    (NotSignedOffError), which is recorded with the time; one already live keeps the
    sign-off it went live with. Leaving live clears it: it is asked for again."""
    if stage == STAGE_LIVE and row.stage != STAGE_LIVE:
        name = " ".join((sign_off.service_lead if sign_off else "").split())
        if not name:
            raise NotSignedOffError
        row.signed_off_by = name[:MAX_SIGN_OFF_NAME]
        row.signed_off_at = datetime.now(UTC)
        row.signed_off_recorded_by = sign_off.recorded_by if sign_off else None
    elif stage != STAGE_LIVE:
        row.signed_off_by = None
        row.signed_off_at = None
        row.signed_off_recorded_by = None
    row.stage = stage


def sign_off_record(row: Jurisdiction | JurisdictionTemplate) -> dict | None:
    """The sign-off it went live with, for the admin area: the service lead and when."""
    if row.signed_off_by is None:
        return None
    at = row.signed_off_at
    return {"service_lead": row.signed_off_by, "signed_off_at": at.isoformat() if at else None}


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
    sign_off: SignOff | None = None,
) -> str:
    """Create or update a jurisdiction. ``enabled`` left as None keeps its current value (a
    new jurisdiction: enabled, a draft), so updating the wording cannot re-enable a disabled
    jurisdiction. So are the stage, the display name and the service (blank: none).

    It leaves draft only when it is ready (NotReadyError): its required screen wording set,
    and an administrator assigned. It goes live only with ``sign_off`` (NotSignedOffError,
    set_stage).

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
        try:
            set_stage(row, stage, sign_off)
        except NotSignedOffError:
            session.rollback()  # the lock
            raise
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
            "sign_off": sign_off_record(row),
            "enabled": row.enabled,
            "settings": row.settings,
            "member_count": counts.get(row.id, 0),
            "problems": readiness_problems(parse_settings(row.settings, row.code), admin_count=admins.get(row.id, 0)),
            "version": version(row),
        }
        for row in session.exec(select(Jurisdiction).order_by(Jurisdiction.name)).all()
    ]


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
    put.add_argument("--signed-off-by", help="the service lead who signed it off: needed to make it live")
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
            sign_off=SignOff(args.signed_off_by) if args.signed_off_by else None,
        )
    msg = f"unknown command {args.command}"
    raise AdminError(msg)


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
