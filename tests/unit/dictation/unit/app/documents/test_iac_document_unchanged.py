"""DIAAT-342: a jurisdiction with legal wording (the IAC) gets exactly the document it got
before legal wording belonged to a jurisdiction.

Four IAC submissions are rendered into real .docx files, and every part of each file
(document body, styles, headers, footers, numbering, properties) is hashed. The expected
hashes in ``golden_iac_documents.json`` were produced by the code of origin/master
before DIAAT-342 (``git archive origin/master src``), running this very module:

    git archive origin/master src | tar -x -C /tmp/master-src
    PYTHONPATH=/tmp/master-src/src:. python \\
        tests/unit/dictation/unit/app/documents/test_iac_document_unchanged.py out.json

The year in the footer is pinned, and the content is the bundled IAC wording.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

GOLDEN = Path(__file__).with_name("golden_iac_documents.json")
PINNED_YEAR = 2026

_BASE_FORM = {
    "caseId": "PA/12345/2026",
    "location": "Taylor House",
    "hearingDate": "2026-03-04",
    "judgeName": "A Judge",
    "anonymityOrder": "granted",
    "appellantName": "A B",
    "respondent": "Secretary of State",
    "hearingType": "face_to_face",
    "appealableDecisionDate": "2026-01-02",
    "documentType": "decision",
}
_MESSAGES = {
    "background": [{"speaker": "Judge", "text": "Background words.", "timestamp": "10:01", "timestampMs": 1000}],
    "evidence": [{"speaker": "Appellant", "text": "Evidence words.", "timestamp": "10:02", "timestampMs": 2000}],
    "facts": [{"speaker": "Judge", "text": "Fact words.", "timestamp": "10:03", "timestampMs": 3000}],
}


def _scenarios(framework_ids: list[str]) -> dict[str, dict]:
    return {
        "frameworks_two": {**_BASE_FORM, "legalIssues": framework_ids[:2]},
        "frameworks_all_rule_28": {
            **_BASE_FORM,
            "legalIssues": framework_ids,
            "appellantRepType": "Not represented",
            "anonymityOrder": "not_sought",
        },
        "no_issues_selected": {**_BASE_FORM, "legalIssues": [], "hearingType": "video"},
        "stale_unknown_issue": {**_BASE_FORM, "legalIssues": ["gone"], "anonymityOrder": "sought_but_refused"},
    }


def render_part_hashes(monkeypatch=None) -> dict[str, dict[str, str]]:
    """Render the four submissions with the bundled IAC wording; {scenario: {part: sha256}}."""
    import transcribe_api
    from transcribe_api.api.routes_dictation import LiveTranscriptionSubmissionRequest
    from transcribe_api.documents import content_loader, hearing_document_router
    from transcribe_api.documents.template_renderer import render_hearing_document

    content = json.loads((Path(transcribe_api.__file__).parent / "data" / "document_content.json").read_text())

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(PINNED_YEAR, 6, 1, tzinfo=tz or UTC)

    patches = [
        (content_loader, "get_document_content", lambda *_args, **_kwargs: content),
        (hearing_document_router, "datetime", _Pinned),
    ]
    saved = [(module, name, getattr(module, name)) for module, name, _ in patches]
    for module, name, value in patches:
        setattr(module, name, value)
    try:
        hashes: dict[str, dict[str, str]] = {}
        with tempfile.TemporaryDirectory() as out:
            for name, form in _scenarios([f["id"] for f in content["legal_frameworks"]]).items():
                request = LiveTranscriptionSubmissionRequest.model_validate(
                    {"form_data": form, "messages": _MESSAGES, "notes": "<p>My note</p>"}
                )
                path = render_hearing_document(request, Path(out), "judge@example.test")
                with zipfile.ZipFile(path) as docx:
                    hashes[name] = {part: hashlib.sha256(docx.read(part)).hexdigest() for part in sorted(docx.namelist())}
        return hashes
    finally:
        for module, name, value in saved:
            setattr(module, name, value)


def test_iac_documents_are_byte_identical_to_those_made_before_jurisdiction_legal_wording():
    expected = json.loads(GOLDEN.read_text())
    actual = render_part_hashes()
    assert set(actual) == set(expected)
    for scenario, parts in expected.items():
        assert actual[scenario] == parts, scenario


def test_the_golden_hashes_cover_the_whole_package_of_each_document():
    expected = json.loads(GOLDEN.read_text())
    for parts in expected.values():
        assert "word/document.xml" in parts
        assert len(parts) > 10


if __name__ == "__main__":  # pragma: no cover - how the golden file is made (see the docstring)
    import tests.unit.conftest  # noqa: F401  (a complete, inert test environment)

    Path(sys.argv[1]).write_text(json.dumps(render_part_hashes(), indent=1, sort_keys=True) + "\n")
