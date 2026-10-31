"""The user's jurisdictions (DIAAT-322): what the web app loads before anything else.

``GET /api/jurisdictions/me`` never fails for a signed-in user: it says whether they
are ready to work (and in which jurisdiction, with its wording), have no jurisdiction
(the web app shows how to ask for access) or must choose one.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from transcribe_api.domain.auth.dependencies_dictation import get_current_user
from transcribe_api.domain.jurisdictions import (
    HEADER,
    NOT_AVAILABLE,
    JurisdictionFeatures,
    Membership,
    choose,
    load_memberships,
)
from transcribe_api.domain.models_dictation import User

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
    summaries = [_summary(m) for m in memberships]
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
        return MyJurisdictionsResponse(status="choose", current=None, jurisdictions=summaries)
    return MyJurisdictionsResponse(status="ready", current=_current(active.current), jurisdictions=summaries)
