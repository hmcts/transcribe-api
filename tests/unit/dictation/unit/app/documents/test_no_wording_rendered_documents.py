"""Real rendered .docx files, read back: a jurisdiction without legal wording gets no legal sections (DIAAT-342).

Both generation paths are rendered for real (no mocked Word document) and every paragraph of the body
(including content controls and tables), headers and footers is searched.
"""

import hashlib
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from docx import Document
from docx.oxml.ns import qn

from transcribe_api.documents import template_renderer
from transcribe_api.documents.content_loader import CONTENT_FILE
from transcribe_api.documents.hearing_document_router import (
    ISSUES_INTRO_TEXT,
    build_hearing_document_from_document_data,
)
from transcribe_api.documents.models import DocumentData, HearingFormData, SectionTranscripts, TranscriptMessage

IAC_WORDING = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))
NO_LEGAL_WORDING = {**IAC_WORDING, "legal_frameworks": []}
WORDING = {"iac": IAC_WORDING, "tax": NO_LEGAL_WORDING}

# Text of the legal sections, in the template path and in the structured path, that must not appear.
LEGAL_SECTION_LINES = ("Issues", "Legal Framework")
LEGAL_SECTION_TEXT = (
    "The parties agreed that the issues the Tribunal must decide are",
    "THIS IS BASED ON A STANDARD LIST OF ISSUES",
    ISSUES_INTRO_TEXT,
    "The following matters were agreed between the parties",
    "[LIST AGREED LEGAL AND FACTUAL POINTS]",
    "Legal Framework from template generator",
    "[No legal issues selected]",
    "[No legal frameworks selected]",
    "[Legal frameworks not found]",
)


@pytest.fixture(autouse=True)
def wording_by_jurisdiction():
    with patch(
        "transcribe_api.documents.content_loader.get_document_content",
        side_effect=lambda jurisdiction_id="iac": WORDING[jurisdiction_id],
    ):
        yield


def _document_data() -> DocumentData:
    form_data = HearingFormData.model_validate(
        {
            "caseId": "PA/12345/2025",
            "location": "Taylor House",
            "hearingDate": "2026-01-23",
            "judgeName": "N SONI",
            "anonymityOrder": "no",
            "appellantName": "ELLIOTT",
            "respondent": "THE SECRETARY OF STATE",
            "hearingType": "face_to_face",
            "appealableDecisionDate": "2025-12-01",
            # Selected by a stale client: ignored without legal wording.
            "legalIssues": ["asylum_pre_naba"],
            "documentType": "Decision",
        }
    )
    messages = SectionTranscripts(
        background=[TranscriptMessage(speaker="Judge", text="Background text", timestamp="10:00:00")],
    )
    return DocumentData(form_data=form_data, messages=messages, user_email="clerk@justice.gov.uk")


def _all_lines(path: Path) -> list[str]:
    """Every paragraph's text: body (content controls and tables included), headers and footers."""
    document = Document(str(path))
    parts = [document.element.body]
    for section in document.sections:
        parts += [
            part._element  # noqa: SLF001
            for part in (section.header, section.footer, section.first_page_header, section.first_page_footer)
        ]
    return [
        "".join(t.text or "" for t in paragraph.iter(qn("w:t"))) for part in parts for paragraph in part.iter(qn("w:p"))
    ]


def _render_template_path(tmp_path: Path, jurisdiction_id: str) -> list[str]:
    path = template_renderer.render_document(_document_data(), tmp_path / "out.docx", jurisdiction_id=jurisdiction_id)
    return _all_lines(path)


def _render_structured_path(tmp_path: Path, jurisdiction_id: str) -> list[str]:
    document = build_hearing_document_from_document_data(_document_data(), jurisdiction_id)
    return _all_lines(template_renderer.render_document(document, tmp_path / "out.docx"))


def _assert_no_legal_sections(lines: list[str]) -> None:
    stripped = [line.strip() for line in lines]
    for heading in LEGAL_SECTION_LINES:
        assert heading not in stripped, f"heading '{heading}' present"
    everything = "\n".join(lines)
    for text in LEGAL_SECTION_TEXT:
        assert text not in everything, f"'{text}' present"
    # The rest of the document is intact.
    for kept in ("Background", "Background text", "Evidence", "Facts", "Determination of the Issues"):
        assert kept in stripped, f"'{kept}' missing"


@pytest.mark.parametrize("render", [_render_template_path, _render_structured_path], ids=["template", "structured"])
def test_a_rendered_document_for_a_jurisdiction_without_legal_wording_has_no_legal_sections(tmp_path, render):
    _assert_no_legal_sections(render(tmp_path, "tax"))


@pytest.mark.parametrize("render", [_render_template_path, _render_structured_path], ids=["template", "structured"])
def test_a_rendered_iac_document_keeps_its_legal_sections_in_order(tmp_path, render):
    lines = [line.strip() for line in render(tmp_path, "iac")]

    for heading in LEGAL_SECTION_LINES:
        assert heading in lines
    assert "The parties agreed that the issues the Tribunal must decide are" in "\n".join(lines)
    assert lines.index("Background") < lines.index("Issues") < lines.index("Evidence")
    assert lines.index("Evidence") < lines.index("Legal Framework") < lines.index("Facts")


def test_the_template_path_does_not_touch_the_template_when_there_is_legal_wording(tmp_path):
    with patch.object(template_renderer, "_remove_legal_sections", side_effect=AssertionError("called")):
        lines = _render_template_path(tmp_path, "iac")

    assert "Issues" in [line.strip() for line in lines]


def test_the_iac_template_path_output_is_unchanged(tmp_path):
    """The IAC document text is what the code rendered before the legal sections could be removed (hash taken from it)."""
    lines = [
        re.sub(r"CROWN COPYRIGHT \d{4}", "CROWN COPYRIGHT YEAR", line)
        for line in _render_template_path(tmp_path, "iac")
    ]
    lines = [line for line in lines if line.strip()]

    assert len(lines) == 55
    assert hashlib.sha256("\n".join(lines).encode()).hexdigest() == (
        "72be4a5b24e44adc63e3354366746bc63c415c334aa201c6287f18c1cb0ea5ee"
    )


def test_only_the_legal_sections_are_removed_from_the_template():
    from docx import Document as WordDocument

    document = WordDocument(str(template_renderer.DECISION_TEMPLATE_PATH))
    body = document.element.body
    before = len(list(body))

    removed = template_renderer._remove_legal_sections(document)  # noqa: SLF001

    assert removed == ["Issues", "Legal Framework"]
    # Issues heading, introduction, control, the two agreed-matters paragraphs; Legal Framework heading and control.
    assert before - len(list(body)) == 7
    tags = {tag.get(qn("w:val")) for tag in body.iter(qn("w:tag"))}
    assert not {"Issues", "LegalFramework"} & tags
    assert {"HearingDescription", "FeeAward"} <= tags
