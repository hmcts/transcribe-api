from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Frontmatter(BaseModel):
    """Structured frontmatter values used to build the first page."""

    model_config = ConfigDict(str_strip_whitespace=True)

    case_id: str | None = None
    crest_path: str | None = None
    tribunal_title: str = Field(default="First-tier Tribunal")
    tribunal_chamber: str = Field(default="(Immigration and Asylum Chamber)")
    appeal_number: str = Field(min_length=1)
    acts_heading: str = Field(default="THE IMMIGRATION ACTS")

    hearing_location_prefix: str = Field(default="Heard at")
    hearing_location: str = Field(min_length=1)
    hearing_date_prefix: str = Field(default="On")
    hearing_date: str = Field(min_length=1)

    before_heading: str = Field(default="Before")
    judge_title: str = Field(default="FIRST-TIER TRIBUNAL JUDGE")
    judge_name: str = Field(min_length=1)

    between_heading: str = Field(default="Between")
    appellant_name: str = Field(min_length=1)
    anonymity_label: str = Field(default="(ANONYMITY ORDER MADE)")
    include_anonymity_label: bool = Field(default=True)
    and_heading: str = Field(default="and")
    respondent_name: str = Field(default="THE SECRETARY OF STATE FOR THE HOME DEPARTMENT")

    appellant_role_label: str = Field(default="Appellant", min_length=1)
    respondent_role_label: str = Field(default="Respondent", min_length=1)

    representation_heading: str = Field(default="Representation:")
    for_appellant_label: str = Field(default="For the Appellant:")
    for_respondent_label: str = Field(default="For the Respondent:")
    appellant_representative: str = Field(default="")
    respondent_representative: str = Field(default="")
    appellant_placeholder: str = Field(default="")

    def appellant_display_name(self) -> str:
        """Build appellant block with optional anonymity line."""
        base = self.appellant_name.upper()
        if self.include_anonymity_label:
            return f"{base}\n{self.anonymity_label}"
        return base


class Paragraph(BaseModel):
    """A paragraph block for structured hearing document rendering."""

    block_type: Literal["paragraph"] = "paragraph"
    text: str
    style: str | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font_size: int | None = None
    alignment: Literal["left", "center", "right", "justify"] | None = None
    left_indent_pt: float | None = None
    first_line_indent_pt: float | None = None
    list_level: int | None = None
    right_tab: bool = False


class Heading(BaseModel):
    """A heading block for structured hearing document rendering."""

    block_type: Literal["heading"] = "heading"
    text: str
    level: int = Field(default=1, ge=0, le=9)
    alignment: Literal["left", "center", "right", "justify"] | None = None
    bold: bool = True
    underline: bool = True
    font_size: int = 12


class PageBreak(BaseModel):
    """A page-break block for structured hearing document rendering."""

    block_type: Literal["page_break"] = "page_break"


ContentBlock = Annotated[
    Paragraph | Heading | PageBreak,
    Field(discriminator="block_type"),
]


class IACTemplate(BaseModel):
    """Structured section ordering for IAC decision documents."""

    model_config = ConfigDict(populate_by_name=True)

    anonymity: list[ContentBlock] = Field(default_factory=list)
    background: list[ContentBlock] = Field(default_factory=list)
    hearing: list[ContentBlock] = Field(default_factory=list)
    issues: list[ContentBlock] = Field(default_factory=list)
    evidence: list[ContentBlock] = Field(default_factory=list)
    legal_framework: list[ContentBlock] = Field(default_factory=list)
    facts: list[ContentBlock] = Field(default_factory=list)
    determination_of_issues: list[ContentBlock] = Field(default_factory=list)
    notice_of_decision: list[ContentBlock] = Field(default_factory=list)
    anonymity_order: list[ContentBlock] = Field(default_factory=list)
    to_the_respondent: list[ContentBlock] = Field(default_factory=list)
    fee_award: list[ContentBlock] = Field(default_factory=list)
    signature: list[ContentBlock] = Field(default_factory=list)

    def ordered_sections(self) -> list[tuple[str, list[ContentBlock]]]:
        return [
            ("anonymity", self.anonymity),
            ("background", self.background),
            ("hearing", self.hearing),
            ("issues", self.issues),
            ("evidence", self.evidence),
            ("legal_framework", self.legal_framework),
            ("facts", self.facts),
            ("determination_of_issues", self.determination_of_issues),
            ("notice_of_decision", self.notice_of_decision),
            ("anonymity_order", self.anonymity_order),
            ("to_the_respondent", self.to_the_respondent),
            ("fee_award", self.fee_award),
            ("signature", self.signature),
        ]


TemplateBody = IACTemplate


class HearingDocument(BaseModel):
    """Structured hearing document sections compiled into a Word document."""

    model_config = ConfigDict(populate_by_name=True)

    header: list[ContentBlock] = Field(default_factory=list)
    frontmatter: Frontmatter | None = None
    body: list[ContentBlock] | TemplateBody = Field(default_factory=list)
    footer: list[ContentBlock] = Field(default_factory=list)
