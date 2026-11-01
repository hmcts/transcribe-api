"""The user's jurisdictions (DIAAT-322): what the web app loads before anything else.

``GET /api/jurisdictions/me`` never fails for a signed-in user: it says whether they
are ready to work (and in which jurisdiction, with its wording), have no jurisdiction
(the web app shows how to ask for access) or must choose one.

``PUT /api/jurisdictions/active`` records the one a judge in several chose to work in
(DIAAT-332): requests that name none use it, and so does their next sign-in.

``/api/jurisdictions/wording`` is for a jurisdiction's own admins (DIAAT-356): the
wording of the jurisdiction they are working in, and only that one.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, StringConstraints
from sqlmodel import Session

from transcribe_api import jurisdictions_admin
from transcribe_api.domain.auth.approles_dictation import get_role
from transcribe_api.domain.auth.dependencies_dictation import (
    get_active_jurisdiction,
    get_allowlisted_user,
    get_current_user,
)
from transcribe_api.domain.jurisdictions import (
    HEADER,
    NOT_AVAILABLE,
    ROLE_ADMIN,
    WORDING_KEYS,
    ActiveJurisdiction,
    JurisdictionFeatures,
    Membership,
    choose,
    load_memberships,
    not_available,
    remember_choice,
    resolve,
)
from transcribe_api.domain.models_dictation import User
from transcribe_api.domain.audit import record_audit
from transcribe_api.runtime.db_connection import get_engine

router = APIRouter(tags=["jurisdictions"])


class JurisdictionSummary(BaseModel):
    code: str
    name: str
    role: str


class CurrentJurisdiction(JurisdictionSummary):
    template: str | None
    features: JurisdictionFeatures
    wording: dict[str, str]


class MyJurisdictionsResponse(BaseModel):
    # ready: working in ``current``; no_jurisdiction: none at all; choose: several and
    # no choice yet.
    status: Literal["ready", "no_jurisdiction", "choose"]
    current: CurrentJurisdiction | None
    jurisdictions: list[JurisdictionSummary]


class ChooseJurisdictionRequest(BaseModel):
    # Stripped before the length check, as the code is matched (choose).
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16)]


def _summary(membership: Membership) -> JurisdictionSummary:
    return JurisdictionSummary(code=membership.code, name=membership.name, role=membership.role)


def _current(membership: Membership) -> CurrentJurisdiction:
    settings = membership.settings
    return CurrentJurisdiction(
        **_summary(membership).model_dump(),
        template=settings.template,
        features=settings.features,
        wording=settings.wording,
    )


# A plain def: FastAPI runs it in the threadpool, as the query is synchronous.
@router.get("/jurisdictions/me", response_model=MyJurisdictionsResponse)
def my_jurisdictions(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> MyJurisdictionsResponse:
    """The signed-in user's jurisdictions and the one this tab works in.

    A header naming a jurisdiction the user no longer has is ignored: the answer is
    then what they would get without it, so the web app can move the tab on.
    """
    response.headers["Cache-Control"] = "no-store"
    memberships = tuple(load_memberships(current_user.id))
    if not memberships:
        return MyJurisdictionsResponse(status="no_jurisdiction", current=None, jurisdictions=[])
    requested = request.headers.get(HEADER)
    try:
        try:
            active = choose(memberships, current_user.active_jurisdiction_id, requested)
        except HTTPException as error:
            if not requested or error.detail["code"] != NOT_AVAILABLE:
                raise
            active = choose(memberships, current_user.active_jurisdiction_id, None)
    except HTTPException:
        # With memberships, the only refusal left is "choose one".
        return MyJurisdictionsResponse(
            status="choose", current=None, jurisdictions=[_summary(m) for m in memberships]
        )
    return _ready(active)


def _ready(active: ActiveJurisdiction) -> MyJurisdictionsResponse:
    return MyJurisdictionsResponse(
        status="ready", current=_current(active.current), jurisdictions=[_summary(m) for m in active.memberships]
    )


# A plain def, as above.
@router.put("/jurisdictions/active", response_model=MyJurisdictionsResponse)
def choose_jurisdiction(
    body: ChooseJurisdictionRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> MyJurisdictionsResponse:
    """Work in ``code``, one of the user's jurisdictions, from now on (DIAAT-332).

    403 ``no_jurisdiction`` or ``jurisdiction_not_available`` as for any request: a
    user cannot choose a jurisdiction they do not belong to.
    """
    response.headers["Cache-Control"] = "no-store"
    active = resolve(current_user.id, None, body.code)
    if not remember_choice(current_user.id, active.id):
        raise not_available()  # the user is gone
    return _ready(active)


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


ADMIN_REQUIRED = "jurisdiction_admin_required"

# A BenchNotes app role as well, as for dictation (routes_dictation.py): a membership
# alone does not let someone in.
_ANY_APP_ROLE = get_allowlisted_user(
    required_roles_any=[get_role("Normal"), get_role("Judge"), get_role("LegalTextManager"), get_role("SystemAdministrator")]
)


def _as_its_admin(
    response: Response,
    _user: User = Depends(_ANY_APP_ROLE),  # noqa: B008
    active: ActiveJurisdiction = Depends(get_active_jurisdiction),  # noqa: B008
) -> ActiveJurisdiction:
    """The request's jurisdiction, if the user is one of its admins. Being an admin in
    one jurisdiction gives nothing in another: the jurisdiction is the one they are
    working in, which is always one of their own."""
    response.headers["Cache-Control"] = "no-store"
    if active.current.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail={"code": ADMIN_REQUIRED, "message": f"Only an admin for {active.code} can change its wording."},
        )
    return active


def _wording(active: ActiveJurisdiction, wording: dict[str, str], version: str) -> JurisdictionWording:
    return JurisdictionWording(
        code=active.code, name=active.current.name, keys=WORDING_KEYS, wording=wording, version=version
    )


def _current_version(code: str) -> str:
    with Session(get_engine()) as session:
        return jurisdictions_admin.current_version(session, code)


# Plain defs, as above.
@router.get("/jurisdictions/wording", response_model=JurisdictionWording)
def jurisdiction_wording(active: ActiveJurisdiction = Depends(_as_its_admin)) -> JurisdictionWording:  # noqa: B008
    """The wording of the jurisdiction the admin is working in."""
    return _wording(active, active.settings.wording, _current_version(active.code))


@router.put("/jurisdictions/wording", response_model=JurisdictionWording)
def update_jurisdiction_wording(
    body: WordingUpdate,
    current_user: User = Depends(get_current_user),  # noqa: B008
    active: ActiveJurisdiction = Depends(_as_its_admin),  # noqa: B008
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
        except jurisdictions_admin.AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        saved_version = jurisdictions_admin.current_version(session, active.code)
    before = active.settings.wording
    changed = sorted(key for key in set(before) | set(saved) if before.get(key) != saved.get(key))
    record_audit(
        "jurisdiction.edit",
        actor_id=current_user.id,
        outcome="success",
        object_type="jurisdiction",
        detail={"code": active.code, "fields": ["wording"], "wording_keys_changed": changed},
    )
    return _wording(active, saved, saved_version)
