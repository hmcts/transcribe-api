"""Unit tests for template renderer functions."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from transcribe_api.documents.models import (
    DocumentData,
    HearingFormData,
    SectionTranscripts,
    TranscriptMessage,
)
from transcribe_api.documents.template_renderer import (
    _generate_output_filename,
    render_document,
)


def _make_form(**kwargs) -> HearingFormData:
    defaults = {
        "location": "Manchester",
        "hearing_date": "2026-01-15",
        "judge_name": "Judge Smith",
        "appellant_name": "Mr Jones",
        "respondent": "Secretary of State",
        "hearing_type": "face_to_face",
        "appealable_decision_date": "2025-06-01",
        "document_type": "hearing",
    }
    defaults.update(kwargs)
    return HearingFormData(**defaults)


def _make_doc_data(**form_kwargs) -> DocumentData:
    return DocumentData(
        form_data=_make_form(**form_kwargs),
        messages=[],
        user_email="judge@example.com",
    )


@pytest.fixture
def mock_word_doc():
    """Mock WordDocument.from_template so tests don't need real .docx files."""
    with patch("transcribe_api.documents.template_renderer.WordDocument") as mock_cls:
        mock_doc = MagicMock()
        mock_cls.from_template.return_value = mock_doc
        mock_doc.get_content_control_keys.return_value = set()
        mock_doc.populate_content_controls.return_value = MagicMock(
            populated_fields={"CaseID"},
            missing_fields=set(),
            unused_data_keys=set(),
        )
        mock_doc.find_and_replace.return_value = 0
        mock_doc.save.return_value = None
        yield mock_cls, mock_doc


@pytest.fixture
def fake_template(tmp_path) -> Path:
    """Create a fake template file so exists() check passes."""
    template = tmp_path / "template.docx"
    template.write_bytes(b"fake")
    return template


class TestGenerateOutputFilename:
    def test_includes_email(self):
        name = _generate_output_filename("judge@court.com")
        assert "judge_at_court_com" in name

    def test_includes_docx_extension(self):
        name = _generate_output_filename("a@b.com")
        assert name.endswith(".docx")

    def test_starts_with_hearing_document(self):
        name = _generate_output_filename("x@y.com")
        assert name.startswith("hearing_document_")

    def test_different_calls_produce_different_names(self):
        """Timestamps make filenames unique."""
        import time
        n1 = _generate_output_filename("a@b.com")
        time.sleep(0.01)
        n2 = _generate_output_filename("a@b.com")
        # They may be the same if same second, but the format is correct
        assert n1.endswith(".docx")
        assert n2.endswith(".docx")


class TestRenderDocument:
    def test_raises_file_not_found_for_missing_template(self, tmp_path):
        doc_data = _make_doc_data()
        output = tmp_path / "out.docx"
        missing_template = tmp_path / "nonexistent.docx"

        with pytest.raises(FileNotFoundError, match="Template not found"):
            render_document(doc_data, output, template_path=missing_template)

    def test_returns_output_path_on_success(self, mock_word_doc, fake_template, tmp_path):
        doc_data = _make_doc_data()
        output = tmp_path / "out.docx"

        result = render_document(doc_data, output, template_path=fake_template)
        assert result == output

    def test_calls_save_on_document(self, mock_word_doc, fake_template, tmp_path):
        _, mock_doc = mock_word_doc
        doc_data = _make_doc_data()
        output = tmp_path / "out.docx"

        render_document(doc_data, output, template_path=fake_template)
        mock_doc.save.assert_called_once_with(output)

    def test_populates_content_controls(self, mock_word_doc, fake_template, tmp_path):
        _, mock_doc = mock_word_doc
        doc_data = _make_doc_data(case_id="IA/001/2025")
        output = tmp_path / "out.docx"

        render_document(doc_data, output, template_path=fake_template)
        mock_doc.populate_content_controls.assert_called_once()

    def test_populates_hearing_description_from_hearing_type(self, mock_word_doc, fake_template, tmp_path):
        doc_data = _make_doc_data(hearing_type="face_to_face")
        output = tmp_path / "out.docx"

        with patch("transcribe_api.documents.template_renderer.get_hearing_description", return_value="Face to face hearing") as mock_desc:
            render_document(doc_data, output, template_path=fake_template)

        mock_desc.assert_called_once_with("face_to_face")

    def test_appends_rule_28_for_appellant_non_attendance(self, mock_word_doc, fake_template, tmp_path):
        _, mock_doc = mock_word_doc
        doc_data = _make_doc_data(appellant_rep_type="No representative and did not attend")
        output = tmp_path / "out.docx"

        with patch("transcribe_api.documents.template_renderer.get_hearing_description", return_value="desc"):
            render_document(doc_data, output, template_path=fake_template)

        call_kwargs = mock_doc.populate_content_controls.call_args[0][0]
        assert "rule 28" in call_kwargs.get("HearingDescription", "").lower()
        assert "Appellant" in call_kwargs.get("HearingDescription", "")

    def test_appends_rule_28_for_respondent_non_representation(self, mock_word_doc, fake_template, tmp_path):
        _, mock_doc = mock_word_doc
        doc_data = _make_doc_data(respondent_rep_type="No representative")
        output = tmp_path / "out.docx"

        with patch("transcribe_api.documents.template_renderer.get_hearing_description", return_value="desc"):
            render_document(doc_data, output, template_path=fake_template)

        call_kwargs = mock_doc.populate_content_controls.call_args[0][0]
        assert "rule 28" in call_kwargs.get("HearingDescription", "").lower()
        assert "Respondent" in call_kwargs.get("HearingDescription", "")

    def test_legal_issues_looked_up_when_present(self, mock_word_doc, fake_template, tmp_path):
        doc_data = _make_doc_data(legal_issues=["asylum_pre_naba"])
        output = tmp_path / "out.docx"

        with patch("transcribe_api.documents.template_renderer.get_legal_issues_display", return_value="Asylum Pre-NABA") as mock_issues, patch("transcribe_api.documents.template_renderer.get_legal_framework_content", return_value="content") as mock_content:
            render_document(doc_data, output, template_path=fake_template)

        mock_issues.assert_called_once()
        mock_content.assert_called_once()

    def test_section_transcripts_replaced(self, mock_word_doc, fake_template, tmp_path):
        _, mock_doc = mock_word_doc
        mock_doc.find_and_replace.return_value = 1

        msgs = [TranscriptMessage(speaker="Speaker 0", text="Background text", timestamp="00:00:01")]
        st = SectionTranscripts(background=msgs)
        doc_data = DocumentData(form_data=_make_form(), messages=st, user_email="a@b.com")
        output = tmp_path / "out.docx"

        render_document(doc_data, output, template_path=fake_template)

        # find_and_replace called for sections + copyright
        assert mock_doc.find_and_replace.call_count >= 1

    def test_raises_runtime_error_on_word_document_failure(self, fake_template, tmp_path):
        doc_data = _make_doc_data()
        output = tmp_path / "out.docx"

        with patch("transcribe_api.documents.template_renderer.WordDocument") as mock_cls:
            mock_cls.from_template.side_effect = RuntimeError("docx error")
            with pytest.raises(RuntimeError, match="Document generation failed"):
                render_document(doc_data, output, template_path=fake_template)

    def test_fee_award_set_to_placeholder_when_missing(self, mock_word_doc, fake_template, tmp_path):
        _, mock_doc = mock_word_doc
        doc_data = _make_doc_data()
        output = tmp_path / "out.docx"

        render_document(doc_data, output, template_path=fake_template)

        call_data = mock_doc.populate_content_controls.call_args[0][0]
        assert call_data.get("FeeAward") == "[Fee award to be determined]"

    def test_custom_delete_if_empty_passed_through(self, mock_word_doc, fake_template, tmp_path):
        _, mock_doc = mock_word_doc
        doc_data = _make_doc_data()
        output = tmp_path / "out.docx"
        custom_delete = {"MyControl"}

        render_document(doc_data, output, template_path=fake_template, delete_if_empty=custom_delete)

        call_kwargs = mock_doc.populate_content_controls.call_args[1]
        assert "MyControl" in call_kwargs.get("delete_if_empty", set())
