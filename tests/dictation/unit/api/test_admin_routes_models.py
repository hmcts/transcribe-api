"""Unit tests for admin route payload models."""

import importlib
from pathlib import Path

import pytest

# The test lives inside tests/unit/api/, which is itself registered as the
# 'api' package by pytest's import machinery.  To import the *source* api
# package (backend/api/document_content_models.py) we have to load the source
# module spec directly, bypassing the cached 'api' namespace.
# MERGE NOTE: source now lives under src/transcribe_api/, not backend/.
_BACKEND_ROOT = str(Path(__file__).resolve().parents[4] / "src" / "transcribe_api")
_spec = importlib.util.spec_from_file_location(
    "_src_document_content_models",
    str(Path(_BACKEND_ROOT) / "api" / "document_content_models.py"),
)
_models_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_models_mod)

HearingTypePayload = _models_mod.HearingTypePayload
LegalFrameworkPayload = _models_mod.LegalFrameworkPayload

from pydantic import ValidationError  # noqa: E402


def test_hearing_type_payload_rejects_invalid_id() -> None:
    with pytest.raises(ValidationError):
        HearingTypePayload(
            id="Invalid-ID",
            title="Face to face",
            hearing_title="The Hearing",
            hearing_description="Valid content",
        )


def test_legal_framework_payload_normalizes_issue_entries() -> None:
    payload = LegalFrameworkPayload(
        id="asylum_pre_naba",
        title="Asylum Pre-NABA",
        rank=1,
        content="Some markdown content",
        issues=["  ", " First issue ", "", "Second issue"],
    )

    assert payload.issues == ["First issue", "Second issue"]
