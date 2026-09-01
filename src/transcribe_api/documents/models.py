"""Pydantic models for document generation and template rendering.

This module defines the data models for Word document content controls,
template field mappings, and hearing form data structures.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_JURISDICTION = "First-tier Tribunal Immigration and Asylum Chamber"


def format_date_long(date_str: str) -> str:
    """Format a date string to long format (e.g., "12 January 2026").

    Args:
        date_str: Date in YYYY-MM-DD format (from HTML date input)

    Returns:
        Date formatted as "D MMMM YYYY" (e.g., "12 January 2026")
        Returns original string if parsing fails.
    """
    if not date_str:
        return ""
    try:
        # Parse date-only string (no timezone needed for date formatting)
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError:
        return date_str
    else:
        # Format as "D MMMM YYYY" (e.g., "12 January 2026")
        day = date_obj.day
        month_year = date_obj.strftime("%B %Y")
        return f"{day} {month_year}"


class ContentControlType(StrEnum):
    """Type of content control identifier in Word documents."""

    TAG = "tag"
    ALIAS = "alias"


class ContentControl(BaseModel):
    """Represents a content control (structured document tag) in a Word document.

    Content controls are placeholders in Word templates that can be
    programmatically populated with data.
    """

    key: str = Field(description="The tag or alias identifier of the control")
    control_type: ContentControlType = Field(description="Whether this is a tag or alias")
    value: str | None = Field(default=None, description="Current value in the control")

    model_config = ConfigDict(frozen=True)


class TemplateField(BaseModel):
    """Defines a mapping between a template control and its data source.

    This model describes how form data maps to template content controls,
    including any transformation logic needed.
    """

    control_key: str = Field(description="The content control tag/alias in the template")
    data_key: str = Field(description="The key path in the source data")
    default: str = Field(default="", description="Default value if data is missing")
    transform: str | None = Field(
        default=None,
        description="Optional transformation: 'join_comma', 'format_date', etc.",
    )

    model_config = ConfigDict(frozen=True)


class HearingFormData(BaseModel):
    """Form data submitted from the live transcription hearing page.

    This mirrors the frontend LiveTranscriptionFormRequest but provides
    a clean interface for the document generation module.
    """

    model_config = ConfigDict(populate_by_name=True)

    # Mapping from frontend display values to internal values for appellant representation
    _APPELLANT_REP_TYPE_MAPPING: dict[str, str] = {
        "No representative and did not attend": "no_rep_no_attend",
        "Representing themselves": "self_rep",
        "Represented": "represented",
    }

    # Mapping from frontend display values to internal values for respondent representation
    _RESPONDENT_REP_TYPE_MAPPING: dict[str, str] = {
        "No representative": "no_rep",
        "Home Office Presenting Officer": "hopo",
        "Counsel": "counsel",
    }

    case_id: str | None = Field(default=None, alias="caseId")
    location: str
    location_other: str | None = Field(default=None, alias="locationOther")
    jurisdiction: str = Field(default=DEFAULT_JURISDICTION)
    hearing_date: str = Field(alias="hearingDate")
    judge_name: str = Field(alias="judgeName")
    anonymity_order: str | None = Field(default=None, alias="anonymityOrder")
    appellant_name: str = Field(alias="appellantName")
    respondent: str
    hearing_type: str = Field(alias="hearingType")
    # Appellant representation fields
    appellant_rep_type: str | None = Field(default=None, alias="appellantRepType")
    appellant_rep_details: str | None = Field(default=None, alias="appellantRepDetails")
    # Respondent representation fields
    respondent_rep_type: str | None = Field(default=None, alias="respondentRepType")
    respondent_rep_name: str | None = Field(default=None, alias="respondentRepName")
    appealable_decision_date: str = Field(alias="appealableDecisionDate")
    legal_issues: list[str] = Field(default_factory=list, alias="legalIssues")
    document_type: str = Field(alias="documentType")
    next_hearing_type: str | None = Field(default=None, alias="nextHearingType")
    next_hearing_adjudicator: str | None = Field(default=None, alias="nextHearingAdjudicator")

    def _get_normalized_appellant_rep_type(self) -> str | None:
        """Normalize appellant rep type from frontend display value to internal value."""
        if not self.appellant_rep_type:
            return None
        return self._APPELLANT_REP_TYPE_MAPPING.get(self.appellant_rep_type, self.appellant_rep_type)

    def _get_normalized_respondent_rep_type(self) -> str | None:
        """Normalize respondent rep type from frontend display value to internal value."""
        if not self.respondent_rep_type:
            return None
        return self._RESPONDENT_REP_TYPE_MAPPING.get(self.respondent_rep_type, self.respondent_rep_type)

    def get_appellant_rep_value(self) -> str:
        """Generate the AppRep template value based on appellant representation type.

        Returns:
            - "No representative and did not attend" for no_rep_no_attend
            - "Representing him or herself" for self_rep
            - The appellant_rep_details value for represented
            - Falls back to appellant_rep_details if appellant_rep_type is not set (backward compat)
        """
        rep_type = self._get_normalized_appellant_rep_type()

        if rep_type == "no_rep_no_attend":
            return "No representative and did not attend"
        if rep_type == "self_rep":
            return "Representing him or herself"
        if rep_type == "represented":
            return self.appellant_rep_details or ""
        # Fallback for backward compatibility with old appellant_rep_name field
        return self.appellant_rep_details or ""

    def requires_appellant_rule_28_text(self) -> bool:
        """Check if Rule 28 text is required for appellant non-attendance.

        Returns True if appellant did not attend and has no representative.
        """
        return self._get_normalized_appellant_rep_type() == "no_rep_no_attend"

    def get_respondent_rep_value(self) -> str:
        """Generate the RespRep template value based on respondent representation type.

        Returns:
            - "No Home Office Presenting Officer" for no_rep
            - "[Name], Home Office Presenting Officer" for hopo
            - "[Name], Counsel" for counsel
            - Falls back to respondent_rep_name if respondent_rep_type is not set (backward compat)
        """
        rep_type = self._get_normalized_respondent_rep_type()

        if rep_type == "no_rep":
            return "No Home Office Presenting Officer"
        if rep_type == "hopo":
            name = self.respondent_rep_name or ""
            return f"{name}, Home Office Presenting Officer" if name else "Home Office Presenting Officer"
        if rep_type == "counsel":
            name = self.respondent_rep_name or ""
            return f"{name}, Counsel" if name else "Counsel"
        # Fallback for backward compatibility with old respondent_rep_name field
        return self.respondent_rep_name or ""

    def requires_respondent_rule_28_text(self) -> bool:
        """Check if Rule 28 text is required for respondent non-representation.

        Returns True if respondent has no representative.
        """
        return self._get_normalized_respondent_rep_type() == "no_rep"

    def to_template_dict(self) -> dict[str, str]:
        """Convert form data to a flat dictionary for template population.

        Returns a dictionary with keys matching the Word template content
        control tags/aliases. Note: Some fields contain raw IDs and need
        further processing in the template renderer to look up actual content.

        Fields that need content lookup in renderer:
        - HearingDescription: Contains hearing_type ID, needs description lookup
        - Issues: Contains raw legal_issues list, needs title lookup
        - LegalFramework: Contains raw legal_issues list, needs content lookup
        """
        # Use location_other if location is "Other", otherwise use location
        effective_location = self.location_other if self.location == "Other" and self.location_other else self.location
        # Get appellant rep value based on the new type field
        appellant_rep_value = self.get_appellant_rep_value()
        # Get respondent rep value based on the new type field
        respondent_rep_value = self.get_respondent_rep_value()

        # Set "For the Appellant/Respondent:" labels only if there's representation data
        for_appellant_label = "For the Appellant:" if appellant_rep_value else ""
        for_respondent_label = "For the Respondent:" if respondent_rep_value else ""

        return {
            "CaseID": self.case_id or "",
            "Location": effective_location,
            "Jurisdiction": self.jurisdiction,
            "Hearingdate": format_date_long(self.hearing_date),
            "Judge": self.judge_name,
            "EndJudge": self.judge_name,
            "Appellant": self.appellant_name,
            "Respondent": self.respondent,
            # Raw IDs - will be replaced with looked-up content in renderer
            "HearingDescription": self.hearing_type,
            "AppRep": appellant_rep_value,
            "RespRep": respondent_rep_value,
            "ForTheAppellant": for_appellant_label,
            "ForTheRespondent": for_respondent_label,
            "AppealableDecDate": format_date_long(self.appealable_decision_date),
            # Raw IDs - will be replaced with proper titles/content in renderer
            "Issues": ", ".join(self.legal_issues) if self.legal_issues else "",
            "LegalFramework": ", ".join(self.legal_issues) if self.legal_issues else "",
            # FeeAward is not in form data - will be handled in renderer
        }


class TranscriptMessage(BaseModel):
    """A single message from a live transcription session."""

    model_config = ConfigDict(populate_by_name=True)

    speaker: str
    text: str
    timestamp: str
    timestamp_ms: int | None = Field(default=None, alias="timestampMs")

    def format_for_document(self) -> str:
        """Format this message for inclusion in a document."""
        return f"{self.speaker} [{self.timestamp}]: {self.text}"


class SectionTranscripts(BaseModel):
    """Transcript messages organized by document section.

    Messages are grouped into three sections that correspond to
    placeholders in the Word template:
    - background: Section 3 - Background information about the case
    - evidence: Section 7 - Evidence that was considered
    - facts: Section 10 - Facts found by the tribunal
    """

    model_config = ConfigDict(populate_by_name=True)

    background: list[TranscriptMessage] = Field(default_factory=list)
    evidence: list[TranscriptMessage] = Field(default_factory=list)
    facts: list[TranscriptMessage] = Field(default_factory=list)

    def format_section(self, section: str) -> str:
        """Format a specific section's messages for document inclusion.

        Args:
            section: One of 'background', 'evidence', or 'facts'

        Returns:
            Formatted transcript text for the section, or empty string if no messages.
        """
        messages = getattr(self, section, [])
        if not messages:
            return ""
        return "\n\n".join(msg.text for msg in messages)

    def get_all_messages(self) -> list[TranscriptMessage]:
        """Get all messages from all sections as a flat list."""
        return self.background + self.evidence + self.facts


class DocumentData(BaseModel):
    """Complete data package for generating a hearing document.

    This combines form data and transcript messages into a single
    structure ready for template population.

    The messages field supports two formats:
    1. SectionTranscripts: Messages organized by section (background, evidence, facts)
    2. list[TranscriptMessage]: Legacy flat list format (will be placed in background section)

    The notes field holds raw TipTap HTML from the editor. When present it is
    parsed by ``notes_parser.parse_notes_html`` and merged per section with
    transcript content during rendering: transcript first, optional ``Your notes``
    label, then notes blocks.
    """

    form_data: HearingFormData
    messages: SectionTranscripts | list[TranscriptMessage] = Field(default_factory=list)
    user_email: str
    notes: str | None = Field(default=None, max_length=200_000)

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, notes: str | None) -> str | None:
        if notes is None:
            return None
        normalized = notes.strip()
        return normalized or None

    def get_section_transcripts(self) -> SectionTranscripts:
        """Get messages as SectionTranscripts, converting if necessary.

        If messages is already a SectionTranscripts, returns it directly.
        If messages is a list, wraps it in a SectionTranscripts with all
        messages in the background section.
        """
        if isinstance(self.messages, SectionTranscripts):
            return self.messages
        # Legacy format: put all messages in background
        return SectionTranscripts(background=self.messages)

    def to_template_dict(self) -> dict[str, str]:
        """Generate the complete template data dictionary.

        Combines form data fields with the formatted transcript.
        Note: Section transcripts are handled separately via find-and-replace
        in the template renderer.
        """
        data = self.form_data.to_template_dict()
        # Keep legacy Transcript field for backwards compatibility
        data["Transcript"] = self.format_transcript()
        return data

    def format_transcript(self) -> str:
        """Format all transcript messages for document inclusion (legacy)."""
        section_transcripts = self.get_section_transcripts()
        all_messages = section_transcripts.get_all_messages()
        if not all_messages:
            return "No transcript available."
        return "\n\n".join(msg.text for msg in all_messages)


class PopulationResult(BaseModel):
    """Result of populating a document template with data."""

    populated_fields: set[str] = Field(default_factory=set)
    missing_fields: set[str] = Field(default_factory=set)
    template_controls: set[str] = Field(default_factory=set)
    unused_data_keys: set[str] = Field(default_factory=set)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def summary(self) -> dict[str, Any]:
        """Return a summary dict suitable for logging."""
        return {
            "populated": sorted(self.populated_fields),
            "missing": sorted(self.missing_fields),
            "template_controls": sorted(self.template_controls),
            "unused_keys": sorted(self.unused_data_keys),
        }


# Template field mappings - defines the expected content controls
# These should match the Word template's content control tags/aliases
# Note: AppRep/ForTheAppellant values are derived from appellant_rep_type and appellant_rep_details
# Note: RespRep/ForTheRespondent values are derived from respondent_rep_type and respondent_rep_name
HEARING_TEMPLATE_FIELDS: list[TemplateField] = [
    TemplateField(control_key="CaseID", data_key="case_id"),
    TemplateField(control_key="Location", data_key="location"),
    TemplateField(control_key="Jurisdiction", data_key="jurisdiction"),
    TemplateField(control_key="Hearingdate", data_key="hearing_date"),
    TemplateField(control_key="Judge", data_key="judge_name"),
    TemplateField(control_key="EndJudge", data_key="judge_name"),
    TemplateField(control_key="AnonymityOrder", data_key="anonymity_order"),
    TemplateField(control_key="Appellant", data_key="appellant_name"),
    TemplateField(control_key="Respondent", data_key="respondent"),
    TemplateField(control_key="HearingDescription", data_key="hearing_type"),
    TemplateField(control_key="AppRep", data_key="appellant_rep_type"),  # Derived via get_appellant_rep_value()
    TemplateField(control_key="RespRep", data_key="respondent_rep_type"),  # Derived via get_respondent_rep_value()
    TemplateField(
        control_key="ForTheAppellant", data_key="appellant_rep_type"
    ),  # Derived via get_appellant_rep_value()
    TemplateField(
        control_key="ForTheRespondent", data_key="respondent_rep_type"
    ),  # Derived via get_respondent_rep_value()
    TemplateField(control_key="AppealableDecDate", data_key="appealable_decision_date"),
    TemplateField(control_key="Issues", data_key="legal_issues", transform="join_comma"),
    TemplateField(control_key="LegalFramework", data_key="legal_issues", transform="join_comma"),
    TemplateField(control_key="Transcript", data_key="transcript"),
]
