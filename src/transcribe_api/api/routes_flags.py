"""What is switched on (DIAAT-388, DIAAT-392), under ``/api``.

``GET /api/flags`` tells the web app which system feature flags are on, so it can hide what is
off (the record and speech pages when dictation is off). Any signed-in user, with or without a
context or a role: it says nothing about anyone. The flags themselves are
domain/feature_flags.py; the admin endpoints that set them are DIAAT-392.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from transcribe_api.domain import feature_flags
from transcribe_api.domain.auth.dependencies_dictation import signed_in_endpoint

router = APIRouter(tags=["flags"])


class FlagsResponse(BaseModel):
    dictation_enabled: bool


# A plain def: the flag lookup is a database read, so FastAPI runs it in the threadpool.
@router.get("/flags", response_model=FlagsResponse)
def get_flags(response: Response, _user=Depends(signed_in_endpoint)) -> FlagsResponse:
    """The system feature flags that affect what the web app shows."""
    response.headers["Cache-Control"] = "no-store"
    return FlagsResponse(dictation_enabled=feature_flags.is_dictation_enabled())
