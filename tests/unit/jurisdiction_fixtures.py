"""Jurisdictions for tests (DIAAT-322): the IAC as migration 0004 creates it."""

from __future__ import annotations

from uuid import UUID

from transcribe_api.domain.jurisdictions import JurisdictionFeatures, JurisdictionSettings, Membership

IAC_JURISDICTION_ID = UUID("6a1c0f3e-3b7d-4c2a-9e51-1ac000000001")


def iac_membership(role: str = "judge") -> Membership:
    return Membership(
        jurisdiction_id=IAC_JURISDICTION_ID,
        code="IAC",
        name="Immigration and Asylum Chamber",
        role=role,
        settings=JurisdictionSettings(template="iac_decision", features=JurisdictionFeatures(hearing_recordings=True)),
    )
