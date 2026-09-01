"""Document generation and template rendering utilities.

This module provides tools for generating Word documents from templates.

Quick Start:
    # For API endpoints - use render_hearing_document
    from transcribe_api.documents import render_hearing_document
    path = render_hearing_document(request, output_dir, user_email)

    # For scripts/testing - use DocumentData + render_document
    from transcribe_api.documents import DocumentData, HearingFormData, render_document
    doc_data = DocumentData(form_data=form_data, messages=messages, user_email=email)
    path = render_document(doc_data, output_path)

    # For custom manipulation - use WordDocument directly
    from transcribe_api.documents import WordDocument
    doc = WordDocument.from_template("template.docx")
    doc.populate_content_controls({"Field": "value"})
    doc.find_and_replace("<<placeholder>>", "text")
    doc.save("output.docx")
"""

from transcribe_api.documents.hearing_document_router import build_hearing_document_from_document_data
from transcribe_api.documents.models import (
    ContentControl,
    ContentControlType,
    DocumentData,
    HearingFormData,
    PopulationResult,
    SectionTranscripts,
    TranscriptMessage,
)
from transcribe_api.documents.template_renderer import (
    render_document,
    render_hearing_document,
    render_hearing_document_structured,
    render_structured_document,
)
from transcribe_api.documents.word_document import WordDocument
from transcribe_api.documents.lib.models import (
    Heading,
    HearingDocument,
    PageBreak,
    Paragraph,
)

__all__ = [
    "ContentControl",
    "ContentControlType",
    "DocumentData",
    "Heading",
    "HearingDocument",
    "HearingFormData",
    "PageBreak",
    "Paragraph",
    "PopulationResult",
    "SectionTranscripts",
    "TranscriptMessage",
    "WordDocument",
    "build_hearing_document_from_document_data",
    "render_document",
    "render_hearing_document",
    "render_hearing_document_structured",
    "render_structured_document",
]
