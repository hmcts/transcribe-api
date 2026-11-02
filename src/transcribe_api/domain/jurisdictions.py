"""Jurisdictions and the admins who may edit each one's document wording (DIAAT-342).

A jurisdiction owns one set of document wording (hearing types, legal frameworks,
anonymity statuses; see documents/content_loader.py). A user holding the
LegalTextManager role may read and edit only the jurisdictions they are mapped to
in jurisdiction_admin. A SystemAdministrator manages the mapping.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from transcribe_api.documents.models import DEFAULT_JURISDICTION, DEFAULT_JURISDICTION_ID
from transcribe_api.domain.models_dictation import Jurisdiction, JurisdictionAdmin, User
from transcribe_api.runtime.db_connection import get_engine

logger = logging.getLogger(__name__)

# The jurisdiction that owns the content that existed before DIAAT-342. A form that
# names no jurisdiction gets this one, so existing clients keep working.
IAC_JURISDICTION_ID = DEFAULT_JURISDICTION_ID
IAC_JURISDICTION_NAME = DEFAULT_JURISDICTION


class UnknownJurisdictionError(Exception):
    """A form names a jurisdiction that does not exist."""


class JurisdictionExistsError(Exception):
    """A jurisdiction with this id or name already exists."""


def empty_wording() -> dict:
    """The wording of a jurisdiction that has none."""
    return {"hearing_types": [], "legal_frameworks": [], "anonymity": {"statuses": []}}


def starting_wording() -> dict:
    """Wording for a new jurisdiction: the shipped hearing types and anonymity text, no legal wording.

    Document generation needs the anonymity statuses, so a new jurisdiction starts
    from the shipped defaults (not from the IAC row, which its admins have edited
    and which another jurisdiction's admins must not inherit). It has no legal
    frameworks: until its admins add some, its judges' form has no legal issues
    step and its documents have no Issues or Legal Framework sections.
    """
    from transcribe_api.documents.content_loader import CONTENT_FILE

    with CONTENT_FILE.open(encoding="utf-8") as f:
        wording = copy.deepcopy(json.load(f))
    wording["legal_frameworks"] = []
    return wording


def list_jurisdictions() -> list[Jurisdiction]:
    with Session(get_engine()) as session:
        return list(session.exec(select(Jurisdiction).order_by(col(Jurisdiction.name))).all())


def get_jurisdiction(jurisdiction_id: str) -> Jurisdiction | None:
    with Session(get_engine()) as session:
        return session.get(Jurisdiction, jurisdiction_id)


def resolve_jurisdiction(value: str | None) -> Jurisdiction | None:
    """Find a jurisdiction by id or display name, ignoring case and surrounding space.

    A missing or blank value means the IAC default. Returns None for a name that
    matches no jurisdiction, which callers must treat as an error rather than
    guess: the wording of another jurisdiction must never fill the gap.
    """
    wanted = (value or "").strip().lower() or IAC_JURISDICTION_NAME.lower()
    if wanted in (IAC_JURISDICTION_ID, IAC_JURISDICTION_NAME.lower()):
        # Migration 0004 creates IAC and nothing deletes or renames it, so the
        # commonest answer needs no query.
        return Jurisdiction(id=IAC_JURISDICTION_ID, name=IAC_JURISDICTION_NAME)
    for jurisdiction in list_jurisdictions():
        if wanted in (jurisdiction.id.lower(), jurisdiction.name.lower()):
            return jurisdiction
    return None


def get_admin_jurisdiction_ids(user_id: UUID) -> set[str]:
    """The ids of the jurisdictions this user is mapped to as an admin."""
    with Session(get_engine()) as session:
        rows = session.exec(select(JurisdictionAdmin.jurisdiction_id).where(JurisdictionAdmin.user_id == user_id))
        return set(rows.all())


# Serialises jurisdiction creation (a transaction-scoped Postgres advisory lock key).
_CREATE_LOCK_KEY = 342_000_001


def create_jurisdiction(jurisdiction_id: str, name: str) -> Jurisdiction:
    """Create a jurisdiction and its starting wording together.

    A form names a jurisdiction by id or name ignoring case, so no id or name may equal
    another's id or name once case is ignored (id against id, name against name, and id
    against name). Creates are serialised and the check is made in the same transaction
    as the insert, so two concurrent creates cannot both pass; the unique indexes on
    lower(id) and lower(name) (migration 0004) back it up. Raises JurisdictionExistsError.
    """
    from transcribe_api.documents.content_loader import _new_content_row

    wording = starting_wording()
    wanted = {jurisdiction_id.lower(), name.lower()}
    with Session(get_engine()) as session:
        try:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CREATE_LOCK_KEY})
            for existing in session.exec(select(Jurisdiction)).all():
                if wanted & {existing.id.lower(), existing.name.lower()}:
                    raise JurisdictionExistsError(jurisdiction_id)
            jurisdiction = Jurisdiction(id=jurisdiction_id, name=name)
            session.add(jurisdiction)
            session.flush()  # the wording's foreign key needs the jurisdiction to exist first
            session.add(_new_content_row(jurisdiction_id, wording, datetime.now(UTC)))
            session.commit()
        except (IntegrityError, JurisdictionExistsError) as exc:
            session.rollback()
            if isinstance(exc, JurisdictionExistsError):
                raise
            raise JurisdictionExistsError(jurisdiction_id) from exc
        session.refresh(jurisdiction)
        return jurisdiction


def list_jurisdiction_admins(jurisdiction_id: str) -> list[User]:
    with Session(get_engine()) as session:
        statement = (
            select(User)
            .join(JurisdictionAdmin, col(JurisdictionAdmin.user_id) == col(User.id))
            .where(JurisdictionAdmin.jurisdiction_id == jurisdiction_id)
            .order_by(col(User.email))
        )
        return list(session.exec(statement).all())


def add_jurisdiction_admin(jurisdiction_id: str, user_id: UUID) -> bool:
    """Map a user to a jurisdiction. False if they already were, including a concurrent request that got there first."""
    statement = (
        insert(JurisdictionAdmin)
        .values(user_id=user_id, jurisdiction_id=jurisdiction_id, created_datetime=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=["user_id", "jurisdiction_id"])
    )
    with Session(get_engine()) as session:
        result = session.execute(statement)
        session.commit()
        return result.rowcount == 1


def remove_jurisdiction_admin(jurisdiction_id: str, user_id: UUID) -> bool:
    """Unmap a user from a jurisdiction. False if they were not mapped."""
    with Session(get_engine()) as session:
        row = session.get(JurisdictionAdmin, (user_id, jurisdiction_id))
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
