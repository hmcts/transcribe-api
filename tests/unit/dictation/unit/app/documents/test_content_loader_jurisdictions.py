"""Per-jurisdiction behaviour of the content loader that needs no database (DIAAT-342)."""

import json
from unittest.mock import patch

import pytest

from transcribe_api.documents.content_loader import (
    CONTENT_FILE,
    ContentLoadError,
    clear_cache,
    get_document_content,
    has_legal_wording,
)
from transcribe_api.domain.jurisdictions import (
    IAC_JURISDICTION_NAME,
    empty_wording,
    resolve_jurisdiction,
    starting_wording,
)

DB_DOWN = patch("transcribe_api.runtime.db_connection.get_engine", side_effect=Exception("db unavailable"))


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


def test_iac_falls_back_to_the_bundled_file_when_the_database_is_down():
    with DB_DOWN:
        content = get_document_content("iac")
        assert has_legal_wording("iac")

    assert content == json.loads(CONTENT_FILE.read_text(encoding="utf-8"))


def test_another_jurisdiction_never_falls_back_to_iac_wording_when_the_database_is_down():
    with DB_DOWN, pytest.raises(ContentLoadError):
        get_document_content("tax")


def test_the_shipped_iac_wording_has_legal_frameworks():
    # IAC without legal wording would change what existing judges see.
    assert len(json.loads(CONTENT_FILE.read_text(encoding="utf-8"))["legal_frameworks"]) == 17


def test_a_new_jurisdiction_starts_with_the_shipped_wording_minus_the_legal_frameworks():
    shipped = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))

    wording = starting_wording()

    assert wording["legal_frameworks"] == []
    assert wording["hearing_types"] == shipped["hearing_types"]
    assert wording["anonymity"] == shipped["anonymity"]
    # Its own copy: editing it must not touch another's.
    wording["hearing_types"].clear()
    assert starting_wording()["hearing_types"] == shipped["hearing_types"]


def test_empty_wording_has_no_legal_wording():
    assert empty_wording()["legal_frameworks"] == []


@pytest.mark.parametrize(
    "named",
    [None, "", "  ", "iac", "IAC", IAC_JURISDICTION_NAME, IAC_JURISDICTION_NAME.upper(), f" {IAC_JURISDICTION_NAME} "],
)
def test_a_form_naming_iac_or_nothing_resolves_to_iac_without_a_query(named):
    # No database is configured here: resolving IAC must not need one.
    resolved = resolve_jurisdiction(named)

    assert resolved is not None
    assert (resolved.id, resolved.name) == ("iac", IAC_JURISDICTION_NAME)
