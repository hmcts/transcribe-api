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

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel
from sqlmodel import Session

from transcribe_api import jurisdiction_templates
from transcribe_api.domain.auth.dependencies_dictation import (
    get_active_jurisdiction,
    require_permission,
)
from transcribe_api.domain.jurisdictions import (
    ActiveJurisdiction,
    JurisdictionFeatures,
)
from transcribe_api.runtime.db_connection import get_engine

router = APIRouter(tags=["jurisdictions"])


class CurrentJurisdiction(BaseModel):
    code: str
    # What users see: the display name, else the name (DIAAT-345).
    name: str
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
    # DIAAT-340: "judge_name": the form starts with the signed-in judge's name.
    prefill: str | None = None


class TemplateSection(BaseModel):
    """A dictation section (DIAAT-416): a box the judge's dictation goes in."""

    tag: str
    label: str


class AvailableTemplate(BaseModel):
    id: str
    name: str
    service: str | None
    fields: list[TemplateFormField]
    # The sections the judge dictates into, in the template's order: with several,
    # the judge chooses which they are dictating.
    sections: list[TemplateSection]
    # DIAAT-339: this version of it, which a draft keeps and its submission names.
    revision: str


def template_forms(
    templates: list[jurisdiction_templates.TemplateVersion], jurisdiction_id: UUID
) -> list[AvailableTemplate]:
    """Each template's Hearing Details form: the fields a judge fills (not those
    BenchNotes fills), in the admin's order. A legal issues field offers the
    jurisdiction's legal frameworks, as its Legal Text Manager keeps them."""
    legal_issues: list[TemplateOption] | None = None
    result = []
    for template in templates:
        form_fields = []
        for f in jurisdiction_templates.judge_fields(template.fields):
            if f.type == "legal_issues":
                if legal_issues is None:
                    frameworks = jurisdiction_templates.legal_frameworks(jurisdiction_id)
                    legal_issues = [TemplateOption(value=i, label=t) for i, t in frameworks.items()]
                options = legal_issues
            else:
                options = [TemplateOption(value=o, label=o) for o in f.options]
            form_fields.append(
                TemplateFormField(
                    tag=f.tag,
                    label=f.label,
                    type=f.type,
                    required=f.required,
                    options=options,
                    prefill=f.prefill,
                )
            )
        sections = [
            TemplateSection(tag=f.tag, label=f.label)
            for f in jurisdiction_templates.dictation_sections(template.fields)
        ]
        result.append(
            AvailableTemplate(
                id=str(template.id),
                name=template.name,
                service=template.service,
                fields=form_fields,
                sections=sections,
                revision=template.revision,
            )
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
        templates = [
            jurisdiction_templates.TemplateVersion.of(row)
            for row in jurisdiction_templates.available_templates(session, active.id, user.id)
        ]
    # Outside the session: the legal frameworks are read in their own.
    return template_forms(templates, active.id)


@router.get("/jurisdictions/templates/{template_id}/revisions/{revision}", response_model=AvailableTemplate)
def template_revision(
    template_id: UUID,
    revision: str = Path(max_length=64),
    user=Depends(require_permission("recording.own", "dictation.use")),
    active: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> AvailableTemplate:
    """DIAAT-339: the template's form as a draft started with it (``revision``), to finish
    that draft once the template has changed or been switched off. 404 when the user
    could never choose that version."""
    with Session(get_engine()) as session:
        template = jurisdiction_templates.template_version(session, active.id, template_id, user.id, revision)
    if template is None:
        raise HTTPException(status_code=404, detail="That version of the template is not available.")
    (form,) = template_forms([template], active.id)
    return form
