"""A document is built from its own jurisdiction's wording, and from none if it has none (DIAAT-342)."""

import copy
from unittest.mock import patch

import pytest

from transcribe_api.documents.hearing_document_router import (
    ISSUES_INTRO_TEXT,
    build_hearing_document_from_document_data,
)
from transcribe_api.documents.models import DocumentData, HearingFormData, SectionTranscripts, TranscriptMessage

IAC = "iac"
OTHER = "employment"
NO_LEGAL = "tax"


def _anonymity() -> dict:
    return {
        "statuses": [
            {
                "id": status,
                "title": status,
                "content": f"{{tag}} anonymity {status}",
                "identifier": "",
                "header": "{tag} ANONYMITY HEAD",
                "order_header": "{tag} order header",
                "order_text": "{tag} order text",
            }
            for status in ("granted", "sought_but_refused", "not_sought")
        ]
    }


def _wording(tag: str, *, frameworks: bool = True) -> dict:
    anonymity = copy.deepcopy(_anonymity())
    for status in anonymity["statuses"]:
        for key, value in status.items():
            status[key] = value.replace("{tag}", tag)
    return {
        "hearing_types": [
            {
                "id": "face_to_face",
                "title": "Face to face",
                "hearing_title": f"{tag} hearing title",
                "hearing_description": f"{tag} hearing description.",
            }
        ],
        "legal_frameworks": (
            [
                {
                    "id": "fw",
                    "title": f"{tag} framework",
                    "rank": 1,
                    "content": f"{tag} framework content.",
                    "issues": [f"{tag} issue one", f"{tag} issue two"],
                }
            ]
            if frameworks
            else []
        ),
        "anonymity": anonymity,
    }


WORDING = {IAC: _wording("IAC"), OTHER: _wording("EMPLOYMENT"), NO_LEGAL: _wording("TAX", frameworks=False)}


@pytest.fixture(autouse=True)
def wording_by_jurisdiction():
    with patch(
        "transcribe_api.documents.content_loader.get_document_content",
        side_effect=lambda jurisdiction_id="iac": WORDING[jurisdiction_id],
    ):
        yield


def _document_data(legal_issues: list[str], anonymity_order: str = "granted") -> DocumentData:
    form_data = HearingFormData.model_validate(
        {
            "caseId": "PA/12345/2025",
            "location": "Taylor House",
            "hearingDate": "2026-01-23",
            "judgeName": "N SONI",
            "anonymityOrder": anonymity_order,
            "appellantName": "ELLIOTT",
            "respondent": "THE SECRETARY OF STATE",
            "hearingType": "face_to_face",
            "appealableDecisionDate": "2025-12-01",
            "legalIssues": legal_issues,
            "documentType": "Decision",
        }
    )
    messages = SectionTranscripts(
        background=[TranscriptMessage(speaker="Judge", text="Background text", timestamp="10:00:00")],
    )
    return DocumentData(form_data=form_data, messages=messages, user_email="clerk@justice.gov.uk")


def _texts(blocks) -> list[str]:
    return [block.text for block in blocks if getattr(block, "text", None)]


def _all_text(document) -> str:
    return "\n".join(_texts(b for _name, blocks in document.body.ordered_sections() for b in blocks))


def test_a_document_uses_its_own_jurisdictions_wording_and_none_of_another():
    document = build_hearing_document_from_document_data(_document_data(["fw"]), OTHER)
    text = _all_text(document)

    assert "EMPLOYMENT hearing description." in text
    assert "EMPLOYMENT hearing title" in text
    assert "EMPLOYMENT framework content." in text
    assert "EMPLOYMENT issue one" in text
    assert "EMPLOYMENT order text" in text
    assert "IAC" not in text


def test_the_default_jurisdiction_is_iac_so_existing_callers_are_unchanged():
    assert _all_text(build_hearing_document_from_document_data(_document_data(["fw"]))) == _all_text(
        build_hearing_document_from_document_data(_document_data(["fw"]), IAC)
    )
    assert "IAC framework content." in _all_text(build_hearing_document_from_document_data(_document_data(["fw"])))


def test_a_jurisdiction_with_legal_wording_keeps_its_issues_and_legal_framework_sections():
    document = build_hearing_document_from_document_data(_document_data(["fw"]), IAC)

    assert "Issues" in _texts(document.body.issues)
    assert ISSUES_INTRO_TEXT in _texts(document.body.issues)
    assert "Legal Framework" in _texts(document.body.legal_framework)


def test_a_jurisdiction_with_legal_wording_still_shows_placeholders_when_the_judge_selects_nothing():
    # Unchanged IAC behaviour: the sections stay, with their placeholders.
    document = build_hearing_document_from_document_data(_document_data([]), IAC)

    assert "[No legal issues selected]" in _texts(document.body.issues)
    assert "[No legal frameworks selected]" in _texts(document.body.legal_framework)


@pytest.mark.parametrize("selected", [[], ["fw"], ["stale_id_from_an_old_form"]])
def test_a_jurisdiction_without_legal_wording_gets_no_issues_or_legal_framework_sections(selected):
    document = build_hearing_document_from_document_data(_document_data(selected), NO_LEGAL)

    assert document.body.issues == []
    assert document.body.legal_framework == []
    text = _all_text(document)
    for absent in ("Issues", "Legal Framework", "[No legal issues selected]", "[No legal frameworks selected]"):
        assert absent not in _texts(b for _name, blocks in document.body.ordered_sections() for b in blocks)
        assert absent not in text.split("\n")
    assert ISSUES_INTRO_TEXT not in text
    # Nothing from another jurisdiction fills the gap, and the rest of the document is intact.
    assert "IAC" not in text
    assert "TAX hearing description." in text
    assert "Background text" in text
    assert "Determination of the Issues" in text
    assert "Facts" in text
    assert "Evidence" in text


def test_wording_is_not_shared_through_the_default_when_the_jurisdiction_is_named():
    iac = _all_text(build_hearing_document_from_document_data(_document_data(["fw"]), IAC))
    other = _all_text(build_hearing_document_from_document_data(_document_data(["fw"]), OTHER))

    assert "EMPLOYMENT" not in iac
    assert "IAC" not in other
