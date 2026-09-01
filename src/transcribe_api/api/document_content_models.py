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


class DocumentContentResponse(BaseModel):
    hearing_types: list[HearingTypePayload] = Field(default_factory=list)
    legal_frameworks: list[LegalFrameworkPayload] = Field(default_factory=list)
    anonymity: AnonymityContentResponse = Field(default_factory=AnonymityContentResponse)
