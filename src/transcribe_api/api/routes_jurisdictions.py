"""The jurisdiction a user is working in (DIAAT-322, DIAAT-390).

Which jurisdiction (and role) a user works as is chosen after sign-in and carried in their
BenchNotes token (api/routes_session.py: ``PUT /api/me/context``, ``GET /api/me/permissions``);
``GET /api/jurisdictions/me`` and ``PUT /api/jurisdictions/active`` that used to say and
change it are gone.

``GET /api/jurisdictions/current`` is the active jurisdiction's own setup: what the web app
loads once a context is chosen (its template, its features and its wording).

A jurisdiction's screen wording is edited only by the JurisdictionSuperAdministrator, from the
Jurisdictions screen (``/api/admin/jurisdictions/{code}/wording``, DIAAT-406); the models it shares are
here.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlmodel import Session

from transcribe_api import jurisdiction_templates
from transcribe_api.documents.content_loader import get_document_content
from transcribe_api.documents.uploaded_templates import CONTENT_TYPES, Prefill, ShowWhen, content_choices
from transcribe_api.domain.auth.dependencies_dictation import (
    get_active_jurisdiction,
    require_permission,
)
from transcribe_api.domain.jurisdictions import (
    ActiveJurisdiction,
    JurisdictionFeatures,
)
from transcribe_api.domain.models_dictation import JurisdictionTemplate
from transcribe_api.runtime.db_connection import get_engine

router = APIRouter(tags=["jurisdictions"])


class CurrentJurisdiction(BaseModel):
    code: str
    # What users see: the display name, else the name (DIAAT-345).
    name: str
    # Always None: the built-in IAC decision is gone, judges choose uploaded templates.
    # Kept for an older release's web pods during a rollout.
    template: str | None
    features: JurisdictionFeatures
    wording: dict[str, str]


# A plain def: FastAPI runs it in the threadpool, as the lookup is synchronous.
@router.get("/jurisdictions/current", response_model=CurrentJurisdiction)
def current_jurisdiction(
    response: Response,
    _user=Depends(require_permission("app.access")),
    active: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> CurrentJurisdiction:
    """The jurisdiction the user is acting in: its template, features and wording. 403
    ``context_required`` before a context is chosen, and ``no_jurisdiction`` for a role
    that has none (the global one)."""
    response.headers["Cache-Control"] = "no-store"
    settings = active.settings
    return CurrentJurisdiction(
        code=active.code,
        name=active.current.shown_name,
        template=settings.template,
        features=settings.features,
        wording=settings.wording,
    )


class JurisdictionWording(BaseModel):
    code: str
    name: str
    # Each key the jurisdiction can set, with what it is for.
    keys: dict[str, str]
    wording: dict[str, str]
    # Sent back with a save: a change made meanwhile is refused (409), not undone.
    version: str


class WordingUpdate(BaseModel):
    wording: dict[str, str]
    version: str | None = None


# DIAAT-344: the uploaded templates a judge can choose ---------------------------------


class TemplateOption(BaseModel):
    value: str
    label: str


class TemplateFormField(BaseModel):
    """One field of the Hearing Details form, as the template's admin set it up."""

    tag: str
    label: str
    type: str
    required: bool
    options: list[TemplateOption]
    # Asked only when this other question's answer is one of these.
    show_when: ShowWhen | None = None
    # What the answer starts as, from who is signed in, for the judge to change.
    prefill: Prefill | None = None


class TemplateSection(BaseModel):
    """A dictated box: a section the judge dictates into, by its name."""

    tag: str
    label: str


class AvailableTemplate(BaseModel):
    id: str
    name: str
    service: str | None
    fields: list[TemplateFormField]
    # In the template's order; the first is where dictation goes until the judge picks.
    sections: list[TemplateSection]


def template_forms(rows: list[JurisdictionTemplate], jurisdiction_id: UUID) -> list[AvailableTemplate]:
    """Each template's Hearing Details form: the fields a judge fills (not those
    BenchNotes fills), in the admin's order. Legal issues, hearing type and anonymity
    offer the jurisdiction's Document Content, as its Legal Text Manager keeps it."""
    choices: dict[str, dict[str, str]] | None = None
    result = []
    for row in rows:
        form_fields = []
        for f in jurisdiction_templates.judge_fields(row):
            if f.type in CONTENT_TYPES:
                if choices is None:
                    choices = content_choices(get_document_content(jurisdiction_id))
                options = [TemplateOption(value=i, label=t) for i, t in choices[f.type].items()]
            else:
                options = [TemplateOption(value=o, label=o) for o in f.options]
            form_fields.append(
                TemplateFormField(
                    tag=f.tag,
                    label=f.label,
                    type=f.type,
                    required=f.required,
                    options=options,
                    show_when=f.show_when,
                    prefill=f.prefill,
                )
            )
        sections = [
            TemplateSection(tag=f.tag, label=f.label)
            for f in jurisdiction_templates.fields_of(row)
            if f.type == "dictated"
        ]
        result.append(
            AvailableTemplate(id=str(row.id), name=row.name, service=row.service, fields=form_fields, sections=sections)
        )
    return result


@router.get("/jurisdictions/templates", response_model=list[AvailableTemplate])
def available_templates(
    user=Depends(require_permission("recording.own", "dictation.use")),
    active: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> list[AvailableTemplate]:
    """The templates the user can choose in the jurisdiction they are working in, with the
    fields each asks for: live ones, and those in test for its test group and
    administrators (DIAAT-347)."""
    with Session(get_engine()) as session:
        rows = jurisdiction_templates.available_templates(session, active.id, user.id)
    # Outside the session: the legal frameworks are read in their own.
    return template_forms(rows, active.id)
