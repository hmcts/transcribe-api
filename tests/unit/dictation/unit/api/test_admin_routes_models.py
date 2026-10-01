"""Unit tests for admin route payload models."""

import pytest
from pydantic import ValidationError

# MERGE NOTE: this used to load the module from an absolute path computed with
# Path(__file__).parents[N], to dodge a name clash between the old top-level
# `api` package and the tests' own `api` directory. The source is now the
# installed `transcribe_api` package, so there is no clash and a plain import
# works — which also means moving this file no longer silently breaks it.
from transcribe_api.api.document_content_models import (
    HearingTypePayload,
    LegalFrameworkPayload,
)


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
