"""DIAAT-279: decision document generation telemetry.

Acceptance criteria covered:
* successful and failed generation attempts are visible and traceable to the
  originating dictation session;
* no document content or case details are included in the telemetry.

Also: started/completed/failed events with duration, the template and its version,
a safe error code that says where it failed, and whether the document became
available to the user.
"""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry

from tests.unit.dictation.unit.api.test_live_transcription_submission_logging import (
    _MINIMAL_BODY,
    PII_APPELLANT,
    PII_JUDGE,
    PII_RESPONDENT,
    PII_TRANSCRIPT,
    _make_user,
    _patch_submission_deps,
)
from transcribe_api.documents import content_loader, versioning

SESSION = "3f2b8c1a-9d4e-4f6a-8b7c-1d2e3f4a5b6c"
CASE_DETAILS = (PII_APPELLANT, PII_RESPONDENT, PII_JUDGE, PII_TRANSCRIPT)


def _submit(client, session=SESSION):
    headers = {"X-Dictation-Session-Id": session} if session else {}
    return client.post("/api/live-transcription/submission", json=_MINIMAL_BODY, headers=headers)


def _failing_document_store(mocker, failure=False):
    blob_manager = AsyncMock()
    # The submission JSON is stored; the generated document is not (False, or raised).
    blob_manager.create_blob_from_file = AsyncMock(side_effect=[True, failure])
    blob_manager.set_blob_metadata = AsyncMock(return_value=True)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=blob_manager)
    context.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("transcribe_api.api.routes_dictation.AsyncAzureBlobManager", return_value=context)


# The template version ---------------------------------------------------------------


def test_template_version_tracks_the_renderer_and_the_content_used(monkeypatch):
    # Restored afterwards: the digest is process-wide.
    monkeypatch.setattr(content_loader, "_last_used_digest", None)
    monkeypatch.setattr(content_loader, "_digested", None)
    content_loader._used({"intro": "one"})
    first = versioning.template_version()
    assert re.fullmatch(r"[0-9a-f]{12}", first)

    content_loader._used({"intro": "two"})  # an admin edits the document content
    assert versioning.template_version() != first

    content_loader._used({"intro": "one"})
    assert versioning.template_version() == first
    monkeypatch.setattr(versioning, "_renderer_digest", "a-different-renderer")  # renderer code changed
    assert versioning.template_version() != first


def test_the_renderer_fingerprint_covers_the_structured_renderer():
    """What actually produces the document: this package's code and the crests,
    not the legacy decision-template.docx."""
    import sys

    import transcribe_api.documents.template_renderer  # noqa: F401 - loads what the renderer uses

    names = {path.name for path in sorted(versioning._PACKAGE_DIR.glob("*.py"))}
    assert {"template_renderer.py", "hearing_document_router.py", "styles.py"} <= names
    lib_files = {path.stem for path in versioning._LIB_DIR.glob("*.py")}
    prefix = "transcribe_api.documents.lib."
    used_lib_modules = {name.removeprefix(prefix) for name in sys.modules if name.startswith(prefix)}
    assert used_lib_modules  # the renderer builds documents with lib modules
    assert used_lib_modules <= lib_files  # and every one of them is fingerprinted
    assert all(asset.exists() for asset in versioning._ASSETS)


def test_no_source_files_means_an_unknown_version(monkeypatch, tmp_path):
    """A bytecode-only install: the crests alone would give a version that never changes."""
    monkeypatch.setattr(versioning, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(versioning, "_LIB_DIR", tmp_path)
    assert versioning._compute_renderer_digest() is None


def test_the_cached_content_is_hashed_once_per_load(monkeypatch):
    monkeypatch.setattr(content_loader, "_last_used_digest", None)
    monkeypatch.setattr(content_loader, "_digested", None)
    dumps = MagicMock(wraps=content_loader.json.dumps)
    monkeypatch.setattr(content_loader.json, "dumps", dumps)
    content = {"intro": "one"}
    for _ in range(3):  # cache hits hand out the same object
        content_loader._used(content)
    assert dumps.call_count == 1
    content_loader._used({"intro": "two"})  # reloaded after an edit
    assert dumps.call_count == 2


def test_a_failed_fingerprint_is_retried_not_cached(monkeypatch):
    monkeypatch.setattr(versioning, "_renderer_digest", None)
    calls = iter([None, "renderer"])
    monkeypatch.setattr(versioning, "_compute_renderer_digest", lambda: next(calls))
    assert versioning.template_version() == "unknown"
    assert versioning.template_version() != "unknown"  # recovered on the next call


# The submission ------------------------------------------------------------------------


def test_a_generated_document_is_traceable_and_reported_available(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    _patch_submission_deps(mocker)
    with capture_telemetry() as telemetry:
        assert _submit(client).status_code == 200
        (completed,) = telemetry.events("document_generation.completed")
        (available,) = telemetry.events("document.available")
        for event in (completed, available):
            assert event["document.template"] == "decision"
            assert re.fullmatch(r"[0-9a-f]{12}|unknown", event["document.template_version"])
        assert completed[c.OUTCOME] == "success"
        assert completed[c.DURATION_MS] >= 0  # generation duration
        # Every event of the submission is traceable to the dictation session.
        submission_events = [e for e in telemetry.events() if e.get(c.JOURNEY_TYPE) == "dictation.submission"]
        assert submission_events
        assert {e.get(c.JOURNEY_PARENT_ID) for e in submission_events} == {SESSION}
        for case_detail in CASE_DETAILS:
            telemetry.assert_no_prohibited_content(case_detail)


def test_a_rendering_failure_says_where_and_is_traceable(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    _patch_submission_deps(mocker)
    mocker.patch("transcribe_api.api.routes_dictation.render_hearing_document", side_effect=RuntimeError(PII_APPELLANT))
    with capture_telemetry() as telemetry:
        assert _submit(client).status_code == 500
        (failed,) = telemetry.events("document_generation.failed")
        assert failed[c.OUTCOME] == "error"
        assert failed[c.ERROR_CODE] == "document.render_failed"
        assert failed["document.template_version"]
        assert failed[c.JOURNEY_PARENT_ID] == SESSION
        assert telemetry.events("document.available") == []
        for case_detail in CASE_DETAILS:
            telemetry.assert_no_prohibited_content(case_detail)


@pytest.mark.parametrize("failure", [False, ConnectionError("storage down")])
def test_a_document_that_cannot_be_stored_is_a_failed_generation(client, as_user, mocker, failure):
    as_user(_make_user(roles=["Judge"]))
    _patch_submission_deps(mocker)
    _failing_document_store(mocker, failure)
    with capture_telemetry() as telemetry:
        assert _submit(client).status_code == 500
        assert telemetry.events("document_generation.completed") == []
        (failed,) = telemetry.events("document_generation.failed")
        assert failed[c.OUTCOME] == "error"
        assert failed[c.ERROR_CODE] == "document.store_failed"
        assert failed["document.template"] == "decision"
        assert failed[c.JOURNEY_PARENT_ID] == SESSION
        assert telemetry.events("submission.failed")[0][c.ERROR_CODE] == "document.store_failed"
        assert telemetry.events("document.available") == []


def test_a_malformed_session_header_is_ignored(client, as_user, mocker):
    as_user(_make_user(roles=["Judge"]))
    _patch_submission_deps(mocker)
    with capture_telemetry() as telemetry:
        assert _submit(client, session="PA/50123/2024").status_code == 200
        assert not [e for e in telemetry.events() if e.get(c.JOURNEY_PARENT_ID)]


def test_a_change_to_a_lib_module_changes_the_renderer_fingerprint(monkeypatch, tmp_path):
    app_dir = tmp_path  # transcribe_api
    (app_dir / "documents" / "lib").mkdir(parents=True)
    (app_dir / "data").mkdir()
    module = app_dir / "documents" / "lib" / "typography.py"
    module.write_text("SIZE = 11\n")
    monkeypatch.setattr(versioning, "_APP_DIR", app_dir)
    monkeypatch.setattr(versioning, "_LIB_DIR", app_dir / "documents" / "lib")
    monkeypatch.setattr(versioning, "_PACKAGE_DIR", app_dir / "documents")
    monkeypatch.setattr(versioning, "_ASSETS", ())
    before = versioning._compute_renderer_digest()
    module.write_text("SIZE = 12\n")
    assert versioning._compute_renderer_digest() != before
