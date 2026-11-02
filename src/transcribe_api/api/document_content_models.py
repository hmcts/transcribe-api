"""Shared API models for editable document content."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HearingTypePayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1)
    hearing_title: str = Field(min_length=1)
    hearing_description: str = Field(min_length=1, max_length=200_000)


class LegalFrameworkPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1)
    rank: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=200_000)
    issues: list[str] = Field(default_factory=list)

    @field_validator("issues")
    @classmethod
    def normalize_issues(cls, issues: list[str]) -> list[str]:
        return [issue.strip() for issue in issues if issue.strip()]


class AnonymityStatusPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=200_000)
    identifier: str = Field(default="")
    header: str = Field(min_length=1)
    order_header: str = Field(min_length=1)
    order_text: str = Field(min_length=1, max_length=200_000)


class AnonymityContentResponse(BaseModel):
    statuses: list[AnonymityStatusPayload] = Field(default_factory=list)


class JurisdictionRef(BaseModel):
    id: str
    name: str


class JurisdictionSummary(JurisdictionRef):
    # False when the jurisdiction has no legal frameworks: its judges' form has no
    # legal issues step and its documents have no Issues or Legal Framework sections.
    has_legal_wording: bool


class AdminJurisdictionSummary(JurisdictionSummary):
    # False for a SystemAdministrator who does not also hold LegalTextManager: they may read, not edit.
    can_edit: bool


class CreateJurisdictionPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)


class JurisdictionAdminItem(BaseModel):
    user_id: str
    email: str
    role: str


class DocumentContentResponse(BaseModel):
    # Whose wording this is. Absent only from the pre-DIAAT-342 unscoped admin routes.
    jurisdiction: JurisdictionRef | None = None
    has_legal_wording: bool = True
    hearing_types: list[HearingTypePayload] = Field(default_factory=list)
    legal_frameworks: list[LegalFrameworkPayload] = Field(default_factory=list)
    anonymity: AnonymityContentResponse = Field(default_factory=AnonymityContentResponse)
