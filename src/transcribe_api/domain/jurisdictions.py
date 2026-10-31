"""Jurisdictions (DIAAT-322): which jurisdiction a user works in, and what it looks like.

Every user belongs to one or more jurisdictions (``jurisdiction_membership``), and
every piece of judicial material belongs to exactly one. A request works in one
jurisdiction, chosen in this order:

1. the ``X-Jurisdiction`` header (a jurisdiction code): the web app sends the one its
   tab is in, so a switch in another tab cannot make this tab save elsewhere;
2. the user's remembered choice (``user.active_jurisdiction_id``);
3. their only jurisdiction.

It must be one the user belongs to and that is enabled. A user with none gets 403
``no_jurisdiction``; a user with several and no choice gets 409
``jurisdiction_choice_required``; a header naming a jurisdiction they do not belong to
gets 403 ``jurisdiction_not_available`` (it names a code, not material).

Membership and settings are rows, so a jurisdiction is added without a deployment
(scripts-jurisdictions.py, until the admin area does it: DIAAT-328).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import update
from sqlmodel import Session, select

from transcribe_api.domain.models_dictation import Jurisdiction, JurisdictionMembership, UploadJurisdiction, User
from transcribe_api.runtime.db_connection import get_engine

logger = logging.getLogger(__name__)

HEADER = "X-Jurisdiction"

ROLE_JUDGE = "judge"
ROLE_ADMIN = "admin"
ROLES = (ROLE_JUDGE, ROLE_ADMIN)

NO_JURISDICTION = "no_jurisdiction"
CHOICE_REQUIRED = "jurisdiction_choice_required"
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
    ):
        if code not in _warned_about:  # once per jurisdiction: this runs on every request
            _warned_about.add(code)
            logger.warning("Jurisdiction %s settings have values this release does not know; ignored", code)
    return JurisdictionSettings.model_construct(
        wording=kept,
        template=template if template == TEMPLATE_IAC_DECISION else None,
        features=JurisdictionFeatures(hearing_recordings=hearing_recordings is True),
    )


@dataclass(frozen=True)
class Membership:
    """One of the user's enabled jurisdictions, with their role in it."""

    jurisdiction_id: UUID
    code: str
    name: str
    role: str
    settings: JurisdictionSettings


@dataclass(frozen=True)
class ActiveJurisdiction:
    """The jurisdiction a request works in, and every one the user may switch to."""

    current: Membership
    memberships: tuple[Membership, ...]

    @property
    def id(self) -> UUID:
        return self.current.jurisdiction_id

    @property
    def code(self) -> str:
        return self.current.code

    @property
    def settings(self) -> JurisdictionSettings:
        return self.current.settings


def _load_memberships(user_id: UUID) -> list[Membership]:
    """The user's enabled jurisdictions, by name."""
    with Session(get_engine()) as session:
        rows = session.exec(
            select(Jurisdiction, JurisdictionMembership.role)
            .join(JurisdictionMembership, JurisdictionMembership.jurisdiction_id == Jurisdiction.id)
            .where(JurisdictionMembership.user_id == user_id, Jurisdiction.enabled)
            .order_by(Jurisdiction.name)
        ).all()
    return [
        Membership(
            jurisdiction_id=jurisdiction.id,
            code=jurisdiction.code,
            name=jurisdiction.name,
            role=role,
            settings=parse_settings(jurisdiction.settings, jurisdiction.code),
        )
        for jurisdiction, role in rows
    ]


def load_memberships(user_id: UUID) -> list[Membership]:
    # Callers bind this name; the lookup behind it is what tests replace
    # (tests/unit/conftest.py every_user_is_an_iac_judge).
    return _load_memberships(user_id)


def enrol_new_user(session: Session, user: User) -> None:
    """Give a user their jurisdictions on first sign-in, in the caller's transaction
    (the user row is flushed, not committed).

    Memberships added by email before they signed in are claimed. With none, they are
    made a judge in every enabled ``auto_enrol`` jurisdiction (the IAC), as anyone with
    an app role could use BenchNotes before jurisdictions. Runs once: a user later
    removed from every jurisdiction is not enrolled again.
    """
    email = (user.email or "").strip().lower()
    claimed = 0
    if email:
        claimed = session.execute(
            update(JurisdictionMembership)
            .where(JurisdictionMembership.user_id.is_(None), JurisdictionMembership.email == email)
            .values(user_id=user.id, email=None)
        ).rowcount
    if claimed:
        logger.info("Claimed pending jurisdiction memberships for user id=%s", user.id)
        return
    auto = session.exec(select(Jurisdiction.id).where(Jurisdiction.auto_enrol, Jurisdiction.enabled)).all()
    for jurisdiction_id in auto:
        session.add(JurisdictionMembership(jurisdiction_id=jurisdiction_id, user_id=user.id, role=ROLE_JUDGE))
    logger.info("Enrolled user id=%s in %d auto-enrol jurisdiction(s)", user.id, len(auto))


def _remembered_jurisdiction_id(user_id: UUID) -> UUID | None:
    with Session(get_engine()) as session:
        return session.exec(select(User.active_jurisdiction_id).where(User.id == user_id)).first()


def record_upload_jurisdiction(transcription_id: UUID, jurisdiction_id: UUID) -> None:
    """Remember the jurisdiction an upload is started in, for the poller."""
    with Session(get_engine()) as session:
        session.merge(UploadJurisdiction(transcription_id=transcription_id, jurisdiction_id=jurisdiction_id))
        session.commit()


def upload_jurisdiction_id(transcription_id: str | UUID | None, user_id: UUID) -> UUID | None:
    """The jurisdiction for an upload's new transcription: the one it was started in,
    if the user still belongs to it, else default_jurisdiction_id (an upload from
    before DIAAT-322, or a file not named by its upload ID)."""
    started_in = None
    if transcription_id is not None:
        with Session(get_engine()) as session:
            started_in = session.exec(
                select(UploadJurisdiction.jurisdiction_id).where(
                    UploadJurisdiction.transcription_id == UUID(str(transcription_id))
                )
            ).first()
    if started_in is not None and any(m.jurisdiction_id == started_in for m in load_memberships(user_id)):
        return started_in
    return default_jurisdiction_id(user_id)


def default_jurisdiction_id(user_id: UUID) -> UUID | None:
    """The jurisdiction for work with no request behind it and no record of where it
    started: the user's remembered choice, else their only jurisdiction, else None
    (the record is then in no jurisdiction, and nobody lists it)."""
    try:
        return choose(tuple(load_memberships(user_id)), _remembered_jurisdiction_id(user_id), None).id
    except HTTPException:
        return None


def remember_choice(user_id: UUID, jurisdiction_id: UUID) -> bool:
    """Store the jurisdiction the user chose (DIAAT-332), for requests that name none
    and their next sign-in. False if there is no such user.

    Not re-checked against their memberships here: a stored choice is only ever used
    while it is one of their enabled jurisdictions (choose), so one an admin removes
    a moment later is ignored, not obeyed."""
    with Session(get_engine()) as session:
        stored = session.execute(
            update(User).where(User.id == user_id).values(active_jurisdiction_id=jurisdiction_id)
        ).rowcount
        session.commit()
    if stored:
        logger.info("User id=%s chose to work in jurisdiction id=%s", user_id, jurisdiction_id)
    return stored == 1


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def not_available() -> HTTPException:
    """403: a jurisdiction the user does not belong to (a code, never material)."""
    return _error(403, NOT_AVAILABLE, "You do not have access to that jurisdiction.")


def choose(
    memberships: tuple[Membership, ...], active_jurisdiction_id: UUID | None, requested_code: str | None
) -> ActiveJurisdiction:
    """Which of the user's jurisdictions a request works in (see the module docstring),
    or the error. No database access."""
    if not memberships:
        raise _error(403, NO_JURISDICTION, "You have not been given access to a jurisdiction.")

    if requested_code:
        wanted = requested_code.strip().upper()
        current = next((m for m in memberships if m.code == wanted), None)
        if current is None:
            raise not_available()
        return ActiveJurisdiction(current=current, memberships=memberships)

    current = next((m for m in memberships if m.jurisdiction_id == active_jurisdiction_id), None)
    if current is None and len(memberships) == 1:
        current = memberships[0]
    if current is None:
        raise _error(409, CHOICE_REQUIRED, "Choose the jurisdiction you are working in.")
    return ActiveJurisdiction(current=current, memberships=memberships)


def resolve(user_id: UUID, active_jurisdiction_id: UUID | None, requested_code: str | None) -> ActiveJurisdiction:
    """The jurisdiction this request works in, from the user's memberships."""
    return choose(tuple(load_memberships(user_id)), active_jurisdiction_id, requested_code)


def jurisdiction_dependency(current_user_dep):
    """A FastAPI dependency giving the request's jurisdiction, bound to an area's
    get_current_user so it shares the request's cached user with get_allowlisted_user
    (domain/auth/dependencies_dictation.py and dependencies_recording.py)."""

    def get_active_jurisdiction(request: Request, current_user: User = Depends(current_user_dep)) -> ActiveJurisdiction:  # noqa: B008
        return resolve(current_user.id, current_user.active_jurisdiction_id, request.headers.get(HEADER))

    return get_active_jurisdiction
