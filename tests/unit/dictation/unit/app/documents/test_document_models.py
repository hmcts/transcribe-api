"""Tests for document data model validation behavior."""

from transcribe_api.documents.models import DocumentData, HearingFormData


def _sample_form_data() -> HearingFormData:
    return HearingFormData.model_validate(
        {
            "caseId": "PA/12345/2025",
            "location": "Taylor House",
            "locationOther": None,
            "jurisdiction": "First-tier Tribunal Immigration and Asylum Chamber",
            "hearingDate": "2026-01-23",
            "judgeName": "N SONI",
            "anonymityOrder": "granted",
            "appellantName": "ELLIOTT",
            "respondent": "THE SECRETARY OF STATE FOR THE HOME DEPARTMENT",
            "hearingType": "face_to_face",
            "appellantRepType": "represented",
            "appellantRepDetails": "Ms. Patel",
            "respondentRepType": "counsel",
            "respondentRepName": "Mr. Thomas",
            "appealableDecisionDate": "2025-12-01",
            "legalIssues": ["asylum_pre_naba"],
            "documentType": "Decision",
        }
    )


def test_document_data_normalizes_blank_notes_to_none() -> None:
    document_data = DocumentData(
        form_data=_sample_form_data(),
        messages=[],
        user_email="court.clerk@justice.gov.uk",
        notes="   ",
    )

    assert document_data.notes is None
