"""The jurisdiction a user is working in (DIAAT-322, DIAAT-390).

Which jurisdiction (and role) a user works as is chosen after sign-in and carried in their
BenchNotes token (api/routes_session.py: ``PUT /api/me/context``, ``GET /api/me/permissions``);
``GET /api/jurisdictions/me`` and ``PUT /api/jurisdictions/active`` that used to say and
change it are gone.

``GET /api/jurisdictions/current`` is the active jurisdiction's own setup: what the web app
loads once a context is chosen (its template, its features and its wording).

``/api/jurisdictions/wording`` is for a jurisdiction's own admins (DIAAT-356): the
wording of the jurisdiction they are working in, and only that one. It needs
``jurisdiction.wording.edit``, which a JurisdictionAdministrator holds in the jurisdiction it acts
in (the jurisdiction is the token's context, so it is in scope by construction); the global
JurisdictionSuperAdministrator has no active jurisdiction to edit here (403 ``no_jurisdiction``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session

from transcribe_api import jurisdiction_templates, jurisdictions_admin
from transcribe_api.domain.audit import record_audit
from transcribe_api.domain.auth.dependencies_dictation import (
    get_active_jurisdiction,
    require_permission,
)
from transcribe_api.domain.jurisdictions import (
    WORDING_KEYS,
    ActiveJurisdiction,
    JurisdictionFeatures,
)
from transcribe_api.domain.models_dictation import JurisdictionTemplate, User
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


def _wording_scope(
    response: Response,
    _user=Depends(require_permission("jurisdiction.wording.edit")),
    active: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> ActiveJurisdiction:
    """The request's jurisdiction, for someone who may edit its wording (``jurisdiction.wording.edit``).
    The permission is the active role's, and applies to the jurisdiction they are working in (the
    token's context, which is always one of their own): holding it in one jurisdiction gives
    nothing in another."""
    response.headers["Cache-Control"] = "no-store"
    return active


def _wording(active: ActiveJurisdiction, wording: dict[str, str], version: str) -> JurisdictionWording:
    return JurisdictionWording(
        code=active.code, name=active.current.shown_name, keys=WORDING_KEYS, wording=wording, version=version
    )


# Plain defs, as above.
@router.get("/jurisdictions/wording", response_model=JurisdictionWording)
def jurisdiction_wording(active: ActiveJurisdiction = Depends(_wording_scope)) -> JurisdictionWording:
    """The wording of the jurisdiction the admin is working in."""
    with Session(get_engine()) as session:
        wording, version = jurisdictions_admin.wording_and_version(session, active.code)
    return _wording(active, wording, version)


@router.put("/jurisdictions/wording", response_model=JurisdictionWording)
def update_jurisdiction_wording(
    body: WordingUpdate,
    active: ActiveJurisdiction = Depends(_wording_scope),
    current_user: User = Depends(require_permission("jurisdiction.wording.edit")),
) -> JurisdictionWording:
    """Replace that jurisdiction's wording; blank text means the default. 400 for an
    unknown key or text that is too long, 409 if it changed since ``version``."""
    with Session(get_engine()) as session:
        try:
            saved = jurisdictions_admin.set_wording(
                session, active.code, body.wording, expected_version=body.version
            )
        except jurisdictions_admin.ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except jurisdictions_admin.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except jurisdictions_admin.AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    before, wording = saved.before, saved.wording
    changed = sorted(key for key in set(before) | set(wording) if before.get(key) != wording.get(key))
    record_audit(
        "jurisdiction.edit",
        actor_id=current_user.id,
        outcome="success",
        object_type="jurisdiction",
        detail={"code": active.code, "fields": ["wording"], "wording_keys_changed": changed},
    )
    return _wording(active, wording, saved.version)


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


class AvailableTemplate(BaseModel):
    id: str
    name: str
    service: str | None
    fields: list[TemplateFormField]


def template_forms(rows: list[JurisdictionTemplate], jurisdiction_id: UUID) -> list[AvailableTemplate]:
    """Each template's Hearing Details form: the fields a judge fills (not those
    BenchNotes fills), in the admin's order. A legal issues field offers the
    jurisdiction's legal frameworks, as its Legal Text Manager keeps them."""
    legal_issues: list[TemplateOption] | None = None
    result = []
    for row in rows:
        form_fields = []
        for f in jurisdiction_templates.judge_fields(row):
            if f.type == "legal_issues":
                if legal_issues is None:
                    frameworks = jurisdiction_templates.legal_frameworks(jurisdiction_id)
                    legal_issues = [TemplateOption(value=i, label=t) for i, t in frameworks.items()]
                options = legal_issues
            else:
                options = [TemplateOption(value=o, label=o) for o in f.options]
            form_fields.append(
                TemplateFormField(tag=f.tag, label=f.label, type=f.type, required=f.required, options=options)
            )
        result.append(AvailableTemplate(id=str(row.id), name=row.name, service=row.service, fields=form_fields))
    return result


@router.get("/jurisdictions/templates", response_model=list[AvailableTemplate])
def available_templates(
    _user=Depends(require_permission("recording.own", "dictation.use")),
    active: ActiveJurisdiction = Depends(get_active_jurisdiction),
) -> list[AvailableTemplate]:
    """The templates the jurisdiction's judges can choose, with the fields each asks for.
    For a judge, in the jurisdiction they are working in."""
    with Session(get_engine()) as session:
        rows = jurisdiction_templates.available_templates(session, active.id)
    # Outside the session: the legal frameworks are read in their own.
    return template_forms(rows, active.id)
