"""Tests for DocumentData -> HearingDocument routing logic."""

from datetime import UTC, datetime

from transcribe_api.documents.anonymity import AnonymityStatus
from transcribe_api.documents.hearing_document_router import ISSUES_INTRO_TEXT, build_hearing_document_from_document_data
from transcribe_api.documents.models import DocumentData, HearingFormData, SectionTranscripts, TranscriptMessage
from transcribe_api.documents.lib.models import IACTemplate


def _sample_document_data(anonymity_order: str = "granted") -> DocumentData:
    form_data = HearingFormData.model_validate(
        {
            "caseId": "PA/12345/2025",
            "location": "Taylor House",
            "locationOther": None,
            "jurisdiction": "First-tier Tribunal Immigration and Asylum Chamber",
            "hearingDate": "2026-01-23",
            "judgeName": "N SONI",
            "anonymityOrder": anonymity_order,
            "appellantName": "ELLIOTT",
            "respondent": "THE SECRETARY OF STATE FOR THE HOME DEPARTMENT",
            "hearingType": "face_to_face",
            "appellantRepType": "represented",
            "appellantRepDetails": "Ms. Patel",
            "respondentRepType": "counsel",
            "respondentRepName": "Mr. Thomas",
            "appealableDecisionDate": "2025-12-01",
            "legalIssues": ["asylum_pre_naba", "credibility"],
            "documentType": "Decision",
        }
    )
    messages = SectionTranscripts(
        background=[TranscriptMessage(speaker="Judge", text="Background text", timestamp="10:00:00")],
        evidence=[TranscriptMessage(speaker="Counsel", text="Evidence text", timestamp="10:10:00")],
        facts=[TranscriptMessage(speaker="Judge", text="Facts text", timestamp="10:20:00")],
    )
    return DocumentData(form_data=form_data, messages=messages, user_email="court.clerk@justice.gov.uk")


def test_build_hearing_document_routes_content_loader_values() -> None:
    hearing_document = build_hearing_document_from_document_data(_sample_document_data())

    assert hearing_document.frontmatter is not None
    assert hearing_document.frontmatter.case_id == "PA/12345/2025"
    assert hearing_document.frontmatter.crest_path is not None
    assert hearing_document.frontmatter.hearing_location == "Taylor House"
    assert hearing_document.frontmatter.hearing_date == "23 January 2026"
    assert hearing_document.frontmatter.appellant_representative == "Ms. Patel"
    assert hearing_document.frontmatter.respondent_representative == "Mr. Thomas, Counsel"
    assert hearing_document.frontmatter.include_anonymity_label is True

    assert isinstance(hearing_document.body, IACTemplate)
    template_body = hearing_document.body
    iac_blocks = (
        template_body.anonymity
        + template_body.background
        + template_body.hearing
        + template_body.issues
        + template_body.evidence
        + template_body.legal_framework
        + template_body.facts
        + template_body.determination_of_issues
        + template_body.notice_of_decision
        + template_body.anonymity_order
        + template_body.to_the_respondent
        + template_body.fee_award
        + template_body.signature
    )
    body_texts = [block.text for block in iac_blocks if getattr(block, "text", None)]
    footer_texts = [block.text for block in hearing_document.footer]

    assert "Anonymity" in body_texts
    assert "DECISION AND REASONS" in body_texts
    assert (
        "This is the decision of the Tribunal in the appeal of the Appellant against "
        "the decision of the Respondent made on 1 December 2025."
    ) in body_texts
    assert "Background" in body_texts
    assert "The Hearing" in body_texts
    assert "Issues" in body_texts
    assert ISSUES_INTRO_TEXT in body_texts
    assert "Evidence" in body_texts
    assert "Legal Framework" in body_texts
    assert "Facts" in body_texts
    assert "Determination of the Issues" in body_texts
    assert "Notice of Decision" in body_texts
    assert "ANONYMITY ORDER" in body_texts
    assert "To the Respondent – Fee Award" in body_texts  # noqa: RUF001
    assert "Fee Award" not in body_texts
    assert "Signature" in body_texts
    assert "Signed\tDate [not final until dated]" in body_texts
    assert "[ADD SIGNATURE]" in body_texts
    assert "First-tier Tribunal Judge N SONI" in body_texts
    assert any("The hearing took place face to face." in text for text in body_texts)
    assert "Taking the claim at its highest, is there a Convention reason?" in body_texts
    assert body_texts.index("Issues") < body_texts.index(ISSUES_INTRO_TEXT)
    assert body_texts.index(ISSUES_INTRO_TEXT) < body_texts.index(
        "Taking the claim at its highest, is there a Convention reason?"
    )
    assert "Background text" in body_texts
    assert "Evidence text" in body_texts
    assert "Facts text" in body_texts
    assert f"\u00a9 CROWN COPYRIGHT {datetime.now(UTC).year}" in footer_texts


def test_build_hearing_document_merges_transcript_and_notes_by_section() -> None:
    document_data = _sample_document_data()
    document_data.messages.facts = []
    document_data.notes = "<h3>Evidence</h3><p>Evidence note one.</p><h3>Facts</h3><p>Facts note one.</p>"

    hearing_document = build_hearing_document_from_document_data(document_data)
    assert isinstance(hearing_document.body, IACTemplate)

    background_texts = [block.text for block in hearing_document.body.background if getattr(block, "text", None)]
    evidence_texts = [block.text for block in hearing_document.body.evidence if getattr(block, "text", None)]
    facts_texts = [block.text for block in hearing_document.body.facts if getattr(block, "text", None)]

    # Transcript-only section keeps transcript content.
    assert "Background text" in background_texts
    assert "Your notes" not in background_texts

    # Transcript + notes section renders transcript first, then "Your notes", then notes.
    assert "Evidence text" in evidence_texts
    assert "Your notes" in evidence_texts
    assert "Evidence note one." in evidence_texts
    assert evidence_texts.index("Evidence text") < evidence_texts.index("Your notes")
    assert evidence_texts.index("Your notes") < evidence_texts.index("Evidence note one.")

    # Notes-only section renders notes and omits transcript.
    assert "Facts text" not in facts_texts
    assert "Facts note one." in facts_texts


def test_build_hearing_document_omits_anonymity_section_for_not_sought() -> None:
    hearing_document = build_hearing_document_from_document_data(_sample_document_data("no"))

    assert isinstance(hearing_document.body, IACTemplate)
    anonymity_texts = [block.text for block in hearing_document.body.anonymity if getattr(block, "text", None)]
    anonymity_order_texts = [
        block.text for block in hearing_document.body.anonymity_order if getattr(block, "text", None)
    ]

    assert "Anonymity" not in anonymity_texts
    assert anonymity_order_texts == []


def test_build_hearing_document_keeps_anonymity_order_for_sought_but_refused() -> None:
    hearing_document = build_hearing_document_from_document_data(
        _sample_document_data(AnonymityStatus.SOUGHT_BUT_REFUSED.value)
    )

    assert isinstance(hearing_document.body, IACTemplate)
    anonymity_order_texts = [
        block.text for block in hearing_document.body.anonymity_order if getattr(block, "text", None)
    ]

    assert "ANONYMITY ORDER" in anonymity_order_texts
