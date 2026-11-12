"""Jurisdictions (DIAAT-322): which jurisdiction a request works in, and what it looks like.

Every piece of judicial material belongs to exactly one jurisdiction. A user acts in one
(jurisdiction, role) context at a time, chosen after sign-in (DIAAT-390) and carried in
the BenchNotes access token (``ctx``; domain/auth/app_token.py), and a request works in
that context's jurisdiction. Nothing else chooses one: not a remembered preference, not a
header (``X-Jurisdiction`` is advisory: a mismatch is 409 ``context_changed``,
domain/auth/context.py), and not "their only jurisdiction".

The jurisdiction must be enabled; a context in a disabled one, or in none (the global
JurisdictionSuperAdministrator has no jurisdiction to work in) is 403. Who holds what is the
``role_assignment`` table (domain/assignments.py).

Settings are rows, so a jurisdiction is added without a deployment
(``python -m transcribe_api.jurisdictions_admin``, and the admin area: DIAAT-356).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import Session, select

from transcribe_api.domain.assignments import claim_pending_assignments
from transcribe_api.domain.auth import context as auth_context
from transcribe_api.domain.auth.context import context_required
from transcribe_api.domain.auth.context import error as _error
from transcribe_api.domain.jurisdiction_stages import STAGE_DRAFT, STAGE_LIVE, STAGE_TEST, STAGES, sees  # noqa: F401
from transcribe_api.domain.models_dictation import (
    Jurisdiction,
    RoleAssignment,
    Transcription,
    UploadJurisdiction,
    User,
)
from transcribe_api.runtime.db_connection import get_engine

logger = logging.getLogger(__name__)

HEADER = "X-Jurisdiction"  # advisory (domain/auth/context.py)

# The IAC as migration 0004 creates it: fixed, so environments agree. Its legal wording
# is the document_content row with id 1 (DIAAT-342), and only the IAC's.
IAC_CODE = "IAC"
IAC_JURISDICTION_ID = UUID("6a1c0f3e-3b7d-4c2a-9e51-1ac000000001")

# The member roles of the command line and the SystemAdministrator's members endpoints
# (jurisdictions_admin), as before DIAAT-382; they stand for the assignments Judge and
# JurisdictionAdministrator.
ROLE_JUDGE = "judge"
ROLE_ADMIN = "admin"
ROLES = (ROLE_JUDGE, ROLE_ADMIN)

# DIAAT-345: a new jurisdiction is set up as a draft, tried by its test group, then
# switched on for every member (models_dictation.Jurisdiction.stage): domain/jurisdiction_stages.py.

NO_JURISDICTION = "no_jurisdiction"
NOT_AVAILABLE = "jurisdiction_not_available"

# The IAC decision document (documents/): the hearing form, submission and the
# document_content it uses. A jurisdiction without a template dictates into the
# general section and copies its text out (DIAAT-333); templates are DIAAT-323.
TEMPLATE_IAC_DECISION = "iac_decision"

# The screen text a jurisdiction sets (DIAAT-330). The web app has a neutral default
# for each; a jurisdiction's settings override it. Keep in step with the web app's
# lib/jurisdiction-wording.ts.
WORDING_KEYS: dict[str, str] = {
    "home.intro": "Introduction under the welcome heading",
    "help.subtitle": "Subtitle of the help page",
    "nav.recordings": "Navigation link and home button for hearing recordings",
    "records.column_title": "Title column of the documents list",
    "records.column_type": "Type column of the documents list, and its filter",
    "records.search_placeholder": "Placeholder of the documents search box",
    "records.search_hint": "Hint for the documents search box (screen readers)",
    "records.filter_label": "Label of the type filter (screen readers)",
    "records.filter_placeholder": "Placeholder of the type filter",
    "records.filter_all": "The type filter's 'all' option",
    "records.no_match": "Shown when nothing matches the search",
    "records.open": "Menu item that opens a document",
    "records.rename_hint": "Hint in the rename dialog",
    "records.no_transcript": "Shown when a document has no saved transcript",
    "autosave.record_name": "What dictation is saved to, e.g. 'case record'",
    "alerts.speech_lost_guidance": "Guidance when the speech service connection is lost",
}

# The screen wording a jurisdiction must set itself before it is switched on (out of
# draft): the rest has a neutral default that suits any jurisdiction (DIAAT-345).
REQUIRED_WORDING_KEYS = (
    "home.intro",
    "help.subtitle",
    "records.column_title",
    "records.column_type",
    "autosave.record_name",
)
# Required too, but only where the jurisdiction uses hearing recordings.
RECORDINGS_WORDING_KEY = "nav.recordings"

_MAX_WORDING_LENGTH = 500


class JurisdictionFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The hearing recordings area (/api/v1). Hearing transcription for other
    # jurisdictions is out of DIAAT-322's scope.
    hearing_recordings: bool = False


class JurisdictionSettings(BaseModel):
    """``jurisdiction.settings``: validated before it is written, read leniently."""

    model_config = ConfigDict(extra="forbid")

    wording: dict[str, str] = Field(default_factory=dict)
    template: Literal["iac_decision"] | None = None
    features: JurisdictionFeatures = Field(default_factory=JurisdictionFeatures)

    @field_validator("wording")
    @classmethod
    def _known_keys_only(cls, wording: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(wording) - set(WORDING_KEYS))
        if unknown:
            msg = f"unknown wording keys: {', '.join(unknown)}"
            raise ValueError(msg)
        too_long = sorted(key for key, value in wording.items() if len(value) > _MAX_WORDING_LENGTH)
        if too_long:
            msg = f"wording longer than {_MAX_WORDING_LENGTH} characters: {', '.join(too_long)}"
            raise ValueError(msg)
        return wording


_warned_about: set[str] = set()


def parse_settings(raw: dict | None, code: str = "") -> JurisdictionSettings:
    """A stored row's settings, read leniently: what this release does not know (a key
    a newer release or a hand edit added, a template it lacks) is dropped and logged,
    and everything else is kept, so one bad value cannot take a jurisdiction's whole
    setup (its template, its wording) away."""
    raw = raw if isinstance(raw, dict) else {}
    wording = raw.get("wording") if isinstance(raw.get("wording"), dict) else {}
    kept = {key: value for key, value in wording.items() if key in WORDING_KEYS and isinstance(value, str)}
    template = raw.get("template")
    features = raw.get("features") if isinstance(raw.get("features"), dict) else {}
    hearing_recordings = features.get("hearing_recordings", False)
    if (
        len(kept) != len(wording)
        or template not in (None, TEMPLATE_IAC_DECISION)
        or not isinstance(hearing_recordings, bool)
    ) and code not in _warned_about:  # once per jurisdiction: this runs on every request
        _warned_about.add(code)
        logger.warning("Jurisdiction %s settings have values this release does not know; ignored", code)
    return JurisdictionSettings.model_construct(
        wording=kept,
        template=template if template == TEMPLATE_IAC_DECISION else None,
        features=JurisdictionFeatures(hearing_recordings=hearing_recordings is True),
    )


def missing_wording(settings: JurisdictionSettings) -> list[str]:
    """The required screen wording ``settings`` leaves blank, as WORDING_KEYS keys."""
    required = [*REQUIRED_WORDING_KEYS]
    if settings.features.hearing_recordings:
        required.append(RECORDINGS_WORDING_KEY)
    return [key for key in WORDING_KEYS if key in required and not settings.wording.get(key, "").strip()]


def readiness_problems(settings: JurisdictionSettings, *, admin_count: int) -> list[str]:
    """What stops a jurisdiction being switched on for its test group or everyone
    (DIAAT-345), in plain words; empty when it is ready."""
    problems = []
    missing = missing_wording(settings)
    if missing:
        problems.append(f"Add the screen wording for: {'; '.join(WORDING_KEYS[key] for key in missing)}.")
    if admin_count == 0:
        problems.append("Assign at least one jurisdiction admin.")
    return problems


@dataclass(frozen=True)
class Membership:
    """The jurisdiction a request works in, with the role the user acts as there."""

    jurisdiction_id: UUID
    code: str
    name: str
    role: str
    settings: JurisdictionSettings
    # DIAAT-345: what users see (the name, unless set), and the stage it is at.
    display_name: str = ""
    stage: str = STAGE_LIVE

    @property
    def shown_name(self) -> str:
        return self.display_name or self.name


@dataclass(frozen=True)
class ActiveJurisdiction:
    """The jurisdiction a request works in (the token's context)."""

    current: Membership

    @property
    def id(self) -> UUID:
        return self.current.jurisdiction_id

    @property
    def code(self) -> str:
        return self.current.code

    @property
    def settings(self) -> JurisdictionSettings:
        return self.current.settings


def enrol_new_user(session: Session, user: User) -> None:
    """On a user's first sign-in, in the caller's transaction (the user row is flushed,
    not committed): give them what was assigned to their email before they signed in.

    Nobody is enrolled automatically any more (DIAAT-373): a new user has no role until
    a JurisdictionAdministrator adds them or approves their request. Runs once, for a
    new user: a user later removed from every jurisdiction is not given anything again.
    """
    claim_pending_assignments(session, user.id, user.email)


def _load_membership(code: str, role: str) -> Membership | None:
    """The enabled jurisdiction ``code``, as the user acts in it as ``role``."""
    with Session(get_engine()) as session:
        row = session.exec(select(Jurisdiction).where(Jurisdiction.code == code, Jurisdiction.enabled)).first()
    if row is None:
        return None
    return Membership(
        jurisdiction_id=row.id,
        code=row.code,
        name=row.name,
        role=role,
        settings=parse_settings(row.settings, row.code),
        display_name=row.display_name or "",
        stage=row.stage,
    )


def active_jurisdiction(user: object) -> ActiveJurisdiction:
    """The jurisdiction the user's request works in, from their context.

    403 ``context_required`` with none chosen; 403 ``no_jurisdiction`` when the context is
    a global role's (no jurisdiction to work in); 403 ``jurisdiction_not_available`` when
    the jurisdiction is disabled or gone. No assignment is read: the token's context was
    checked against the assignments when it was minted."""
    ctx = auth_context.context_of(user)
    if ctx is None:
        raise context_required()
    if ctx.jurisdiction is None:
        raise _error(403, NO_JURISDICTION, "The role you are working as has no jurisdiction.")
    membership = _load_membership(ctx.jurisdiction, ctx.role)
    if membership is None:
        raise not_available()
    return ActiveJurisdiction(current=membership)


_jurisdiction_ids: dict[str, UUID] = {}


def known_jurisdiction_id(code: str) -> UUID | None:
    """The ID of a jurisdiction already looked up by code (a code never changes ID), else None."""
    return _jurisdiction_ids.get(code)


def jurisdiction_id_for_code(code: str) -> UUID | None:
    """The jurisdiction's ID, by code, for stamping audit rows. Looked up once a process (a code
    keeps its ID for good); None if there is no such jurisdiction or the lookup fails (an audit
    stamp never fails a request)."""
    known = _jurisdiction_ids.get(code)
    if known is not None:
        return known
    try:
        with Session(get_engine()) as session:
            found = session.exec(select(Jurisdiction.id).where(Jurisdiction.code == code)).first()
    except Exception:
        logger.warning("Could not look up the ID of jurisdiction %s for the audit stamp", code)
        return None
    if found is not None:
        _jurisdiction_ids[code] = found
    return found


def _owned_transcription_jurisdiction(transcription_id: UUID, user_id: UUID) -> tuple[bool, UUID | None]:
    with Session(get_engine()) as session:
        row = session.exec(
            select(Transcription.id, Transcription.jurisdiction_id).where(
                Transcription.id == transcription_id, Transcription.user_id == user_id
            )
        ).first()
    return (False, None) if row is None else (True, row[1])


def owned_transcription_jurisdiction(transcription_id: UUID, user_id: UUID) -> tuple[bool, UUID | None]:
    """DIAAT-334: whether the user owns this transcription, and its jurisdiction (None
    for a record in none). (False, None) when it is not theirs or does not exist: the
    ownership checks answer for those."""
    return _owned_transcription_jurisdiction(transcription_id, user_id)


def _job_owner_and_jurisdiction(job_id: UUID) -> tuple[bool, UUID | None, UUID | None]:
    from transcribe_api.domain.models_recording import SpeechBatchJob  # recording's model

    with Session(get_engine()) as session:
        row = session.exec(
            select(SpeechBatchJob.user_id, SpeechBatchJob.jurisdiction_id).where(SpeechBatchJob.id == job_id)
        ).first()
    return (False, None, None) if row is None else (True, row[0], row[1])


def job_owner_and_jurisdiction(job_id: UUID) -> tuple[bool, UUID | None, UUID | None]:
    """DIAAT-334: whether the recording job exists, its owner and its jurisdiction."""
    return _job_owner_and_jurisdiction(job_id)


def _record_upload_jurisdiction(upload_id: UUID, jurisdiction_id: UUID) -> None:
    with Session(get_engine()) as session:
        session.merge(UploadJurisdiction(transcription_id=upload_id, jurisdiction_id=jurisdiction_id))
        session.commit()


def record_upload_jurisdiction(transcription_id: UUID, jurisdiction_id: UUID) -> None:
    """Remember the jurisdiction an upload is started in: for the poller (dictation),
    and for the job a recording upload is submitted as (DIAAT-334)."""
    _record_upload_jurisdiction(transcription_id, jurisdiction_id)


def _recorded_upload_jurisdiction(upload_id: UUID) -> UUID | None:
    with Session(get_engine()) as session:
        return session.exec(
            select(UploadJurisdiction.jurisdiction_id).where(UploadJurisdiction.transcription_id == upload_id)
        ).first()


def recorded_upload_jurisdiction(upload_id: str | UUID) -> UUID | None:
    """The jurisdiction an upload was started in, if it was recorded."""
    return _recorded_upload_jurisdiction(UUID(str(upload_id)))


def upload_jurisdiction_id(transcription_id: str | UUID | None, user_id: UUID) -> UUID | None:
    """The jurisdiction for an upload's new transcription: the one it was started in,
    whatever the user's memberships now (DIAAT-334: material stays in its own
    jurisdiction; a user removed from it meanwhile does not take it to another, and
    has it back if added again). default_jurisdiction_id only for an upload with no
    record (from before DIAAT-322, or a file not named by its upload ID)."""
    started_in = recorded_upload_jurisdiction(transcription_id) if transcription_id is not None else None
    return started_in if started_in is not None else default_jurisdiction_id(user_id)


def _assigned_jurisdiction_ids(user_id: UUID) -> list[UUID]:
    """The enabled jurisdictions the user holds a role in."""
    with Session(get_engine()) as session:
        return list(
            session.exec(
                select(Jurisdiction.id)
                .join(RoleAssignment, RoleAssignment.jurisdiction_id == Jurisdiction.id)
                .where(RoleAssignment.user_id == user_id, Jurisdiction.enabled)
                .distinct()
            ).all()
        )


def default_jurisdiction_id(user_id: UUID) -> UUID | None:
    """The jurisdiction for work with no request behind it and no record of where it
    started (an upload made before DIAAT-322, or a file not named by its upload ID): the
    user's only jurisdiction, else None (the record is then in no jurisdiction, and nobody
    lists it). There is no remembered choice any more: the active context is per session."""
    ids = _assigned_jurisdiction_ids(user_id)
    return ids[0] if len(ids) == 1 else None


def not_available() -> HTTPException:
    """403: a jurisdiction the user cannot work in (a code, never material)."""
    return _error(403, NOT_AVAILABLE, "You do not have access to that jurisdiction.")


def jurisdiction_dependency(current_user_dep):
    """A FastAPI dependency giving the request's jurisdiction, bound to an area's
    get_current_user so it shares the request's cached user with require_permission
    (domain/auth/dependencies_dictation.py and dependencies_recording.py)."""

    def get_active_jurisdiction(current_user: User = Depends(current_user_dep)) -> ActiveJurisdiction:
        return active_jurisdiction(current_user)

    return get_active_jurisdiction
