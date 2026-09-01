"""Routing logic to convert DocumentData into HearingDocument blocks."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from transcribe_api.documents.anonymity import DELETE_MARKER, AnonymityStatus, get_anonymity_content, parse_anonymity_status
from transcribe_api.documents.content_loader import (
    get_hearing_description,
    get_hearing_title,
    get_legal_framework_content_blocks,
    get_legal_framework_issues,
)
from transcribe_api.documents.models import DocumentData, format_date_long
from transcribe_api.documents.notes_parser import parse_notes_html
from transcribe_api.documents.lib.blocks import (
    enumerated_list_blocks,
    paragraph_blocks,
    section_heading_blocks,
)
from transcribe_api.documents.lib.markdown_to_blocks import markdown_to_blocks
from transcribe_api.documents.lib.models import ContentBlock, Frontmatter, HearingDocument, IACTemplate, Paragraph

# MERGE NOTE: was three parents up (backend/) under the old layout, which
# resolves to src/ here. data/ ships inside the package so it is present in a
# wheel/container too.
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "data"
ENGLISH_CREST_PATH = TEMPLATE_DIR / "english-judicial-decision-crest.png"
SCOTTISH_CREST_PATH = TEMPLATE_DIR / "scottish-judicial-decision-crest.png"
SCOTTISH_LOCATIONS: set[str] = {"Glasgow"}
ISSUES_INTRO_TEXT = (
    "The parties agreed that the issues the Tribunal must decide are: "
    "[THIS IS BASED ON A STANDARD LIST OF ISSUES BASED ON THE LEGAL ISSUES YOU HAVE SELECTED. "
    "IT MUST BE TAILORED TO THE ACTUAL ISSUES AGREED AT THE HEARING]"
)


def _as_content_blocks(paragraphs: list[Paragraph]) -> list[ContentBlock]:
    return [*paragraphs]


def _build_hearing_description_blocks(document_data: DocumentData) -> list[ContentBlock]:
    """Build the hearing description as parsed Markdown blocks."""
    form_data = document_data.form_data
    hearing_md = get_hearing_description(form_data.hearing_type)

    if form_data.requires_appellant_rule_28_text():
        hearing_md += (
            "\n\nThe Appellant failed to attend the hearing. "
            "I considered rule 28 of the Tribunal Procedure (First-tier Tribunal) "
            "(Immigration and Asylum Chamber) Rules 2014. "
            "I was satisfied that [the Appellant had actual notice of the hearing / "
            "reasonable steps had been taken to notify the Appellant of the hearing] "
            "because [REASONS]. It was in the interests of justice to proceed "
            "without the Appellant because [REASONS]."
        )

    if form_data.requires_respondent_rule_28_text():
        hearing_md += (
            "\n\nThe Respondent was not represented at the hearing. "
            "I considered rule 28 of the Tribunal Procedure (First-tier Tribunal) "
            "(Immigration and Asylum Chamber) Rules 2014. "
            "I was satisfied that [the Respondent had actual notice of the hearing / "
            "reasonable steps had been taken to notify the Respondent of the hearing] "
            "because [REASONS]. It was in the interests of justice to proceed "
            "without the Respondent because [REASONS]."
        )

    return markdown_to_blocks(hearing_md)


def _build_legal_framework_blocks(framework_ids_or_titles: list[str]) -> list[ContentBlock]:
    """Build structured blocks for the Legal Framework section from Markdown."""
    blocks = get_legal_framework_content_blocks(framework_ids_or_titles)
    if not blocks:
        return _as_content_blocks(paragraph_blocks("[No legal frameworks selected]", alignment="justify"))
    return blocks


def _build_frontmatter_model(document_data: DocumentData) -> Frontmatter:
    form_data = document_data.form_data
    effective_location = (
        form_data.location_other if form_data.location == "Other" and form_data.location_other else form_data.location
    )
    hearing_date = format_date_long(form_data.hearing_date)
    anonymity_status = parse_anonymity_status(form_data.anonymity_order)
    anonymity_content = get_anonymity_content(anonymity_status)
    include_anonymity_label = anonymity_content.head != DELETE_MARKER

    return Frontmatter(
        case_id=form_data.case_id,
        tribunal_title="First-tier Tribunal",
        tribunal_chamber="(Immigration and Asylum Chamber)",
        appeal_number=form_data.case_id or "TST-123",
        hearing_location=effective_location,
        hearing_date=hearing_date,
        judge_name=form_data.judge_name,
        appellant_name=form_data.appellant_name,
        include_anonymity_label=include_anonymity_label,
        anonymity_label=anonymity_content.head if include_anonymity_label else "(ANONYMITY ORDER MADE)",
        respondent_name=form_data.respondent.upper(),
        appellant_representative=form_data.get_appellant_rep_value(),
        respondent_representative=form_data.get_respondent_rep_value(),
    )


def build_hearing_document_from_document_data(document_data: DocumentData) -> HearingDocument:
    """Convert template-oriented DocumentData into structured HearingDocument."""
    form_data = document_data.form_data
    section_transcripts = document_data.get_section_transcripts()

    frontmatter = _build_frontmatter_model(document_data)
    frontmatter.crest_path = str(
        SCOTTISH_CREST_PATH if frontmatter.hearing_location in SCOTTISH_LOCATIONS else ENGLISH_CREST_PATH
    )
    anonymity_status = parse_anonymity_status(form_data.anonymity_order)
    anonymity_content = get_anonymity_content(anonymity_status)

    header_blocks: list[Paragraph] = []

    anonymity_text = (
        anonymity_content.reasons_text
        if anonymity_content.reasons_text != DELETE_MARKER
        else "No anonymity direction was sought or made."
    )
    include_anonymity_order_section = anonymity_status in {
        AnonymityStatus.GRANTED,
        AnonymityStatus.SOUGHT_BUT_REFUSED,
    }
    anonymity_order_blocks = (
        section_heading_blocks("ANONYMITY ORDER", level=1, bold=True, underline=True)
        + paragraph_blocks(
            anonymity_content.order_text
            if anonymity_content.order_text != DELETE_MARKER
            else "No anonymity order made.",
            alignment="justify",
        )
        if include_anonymity_order_section
        else []
    )

    # Parse notes HTML into per-section paragraph blocks when provided.
    notes_blocks: dict[str, list[Paragraph]] = {}
    if document_data.notes:
        notes_blocks = parse_notes_html(document_data.notes)

    def _section_blocks(section: str, transcript_texts: list[str], placeholder: str) -> list[ContentBlock]:
        """Compose section blocks from transcript and notes with stable ordering."""
        section_notes = notes_blocks.get(section, [])
        has_transcript = len(transcript_texts) > 0
        has_notes = len(section_notes) > 0

        if has_transcript and has_notes:
            return _as_content_blocks(
                [
                    *paragraph_blocks(transcript_texts, alignment="justify"),
                    Paragraph(text="Your notes", bold=True, alignment="justify"),
                    *section_notes,
                ]
            )

        if has_transcript:
            return _as_content_blocks(paragraph_blocks(transcript_texts, alignment="justify"))

        if has_notes:
            return _as_content_blocks(section_notes)

        return _as_content_blocks(paragraph_blocks(placeholder, alignment="justify"))

    background_messages = [msg.text for msg in section_transcripts.background]
    evidence_messages = [msg.text for msg in section_transcripts.evidence]
    facts_messages = [msg.text for msg in section_transcripts.facts]

    legal_issues = get_legal_framework_issues(form_data.legal_issues) if form_data.legal_issues else []
    decision_date_text = (
        format_date_long(form_data.appealable_decision_date) if form_data.appealable_decision_date else "<date>"
    )

    iac_template = IACTemplate(
        anonymity=section_heading_blocks(
            "DECISION AND REASONS",
            level=1,
            bold=True,
            underline=True,
            alignment="center",
        )
        + paragraph_blocks(
            (
                "This is the decision of the Tribunal in the appeal of the Appellant against "
                f"the decision of the Respondent made on {decision_date_text}."
            ),
            alignment="justify",
        )
        + (
            section_heading_blocks("Anonymity", level=1, bold=True)
            + paragraph_blocks(anonymity_text, alignment="justify")
            if anonymity_status != AnonymityStatus.NOT_SOUGHT
            else []
        ),
        background=section_heading_blocks("Background", level=1, bold=True)
        + _section_blocks("background", background_messages, "[No background content]"),
        hearing=section_heading_blocks(get_hearing_title(form_data.hearing_type), level=1, bold=True)
        + _build_hearing_description_blocks(document_data),
        issues=section_heading_blocks("Issues", level=2, bold=True)
        + paragraph_blocks(ISSUES_INTRO_TEXT, alignment="justify")
        + (
            enumerated_list_blocks(legal_issues, style="alphabet")
            if legal_issues
            else paragraph_blocks("[No legal issues selected]", alignment="justify")
        ),
        evidence=section_heading_blocks("Evidence", level=2, bold=True)
        + _section_blocks("evidence", evidence_messages, "[No evidence content]"),
        legal_framework=section_heading_blocks("Legal Framework", level=1, bold=True)
        + _build_legal_framework_blocks(form_data.legal_issues or []),
        facts=section_heading_blocks("Facts", level=1, bold=True)
        + _section_blocks("facts", facts_messages, "[No facts content]"),
        determination_of_issues=section_heading_blocks("Determination of the Issues", level=1, bold=True)
        + paragraph_blocks("[DETERMINATION OF THE ISSUES]", alignment="justify"),
        notice_of_decision=section_heading_blocks("Notice of Decision", level=1, bold=True, underline=True)
        + paragraph_blocks("[Notice of Decision]", alignment="justify"),
        anonymity_order=anonymity_order_blocks,
        to_the_respondent=section_heading_blocks("To the Respondent – Fee Award", level=1, bold=True, underline=True)  # noqa: RUF001
        + paragraph_blocks("[To the Respondent]", alignment="justify"),
        fee_award=paragraph_blocks("[Fee award to be determined]", alignment="justify"),
        signature=[
            *section_heading_blocks("Signature", level=1, bold=True),
            Paragraph(text="Signed\tDate [not final until dated]", right_tab=True),
            Paragraph(text="[ADD SIGNATURE]", italic=True),
            Paragraph(text=f"First-tier Tribunal Judge {form_data.judge_name}"),
        ],
    )

    footer_blocks = [Paragraph(text=f"\u00a9 CROWN COPYRIGHT {datetime.now(UTC).year}", alignment="center")]

    return HearingDocument(
        header=header_blocks,
        frontmatter=frontmatter,
        body=iac_template,
        footer=footer_blocks,
    )
