"""Unit tests for document content loader."""

import json
from unittest.mock import MagicMock, patch

import pytest

from transcribe_api.documents.content_loader import (
    ContentLoadError,
    _load_content_file,
    clear_cache,
    get_anonymity_status_by_id,
    get_anonymity_statuses,
    get_hearing_description,
    get_hearing_title,
    get_hearing_type_by_id,
    get_hearing_type_by_title,
    get_hearing_types,
    get_legal_framework_by_id,
    get_legal_framework_by_title,
    get_legal_framework_content,
    get_legal_framework_issues,
    get_legal_frameworks,
    get_legal_issues_display,
)

SAMPLE_CONTENT = {
    "hearing_types": [
        {
            "id": "face_to_face",
            "title": "Face to face",
            "hearing_title": "The Hearing",
            "hearing_description": "The hearing took place face to face.",
        },
        {
            "id": "video",
            "title": "Video",
            "hearing_title": "The Video Hearing",
            "hearing_description": "The hearing took place by video.",
        },
        {
            "id": "without_hearing",
            "title": "Without a hearing",
            "hearing_title": "Proceeding Without A Hearing",
            "hearing_description": "This decision was made without a hearing.",
        },
    ],
    "legal_frameworks": [
        {
            "id": "asylum_pre_naba",
            "title": "Asylum Pre-NABA",
            "rank": 1,
            "content": "Asylum pre-NABA content.",
            "issues": ["Refugee Convention issue", "Human rights issue"],
        },
        {
            "id": "eea",
            "title": "EEA",
            "rank": 2,
            "content": "EEA content.",
            "issues": ["EEA issue A"],
        },
    ],
    "anonymity": {
        "statuses": [
            {
                "id": "granted",
                "title": "Granted",
                "content": "Order content",
                "identifier": "Yes",
                "header": "ANONYMITY ORDER MADE",
                "order_header": "Order Header",
                "order_text": "The full order text.",
            },
            {
                "id": "not_sought",
                "title": "Not sought",
                "content": "",
                "identifier": "",
                "header": "[DELETE]",
                "order_header": "[DELETE]",
                "order_text": "[DELETE]",
            },
        ]
    },
}


@pytest.fixture(autouse=True)
def patch_content_file():
    """Patch _load_content_file and reset the TTL cache for each test."""
    from transcribe_api.documents.content_loader import clear_cache
    clear_cache()
    with patch("transcribe_api.documents.content_loader.get_document_content", return_value=SAMPLE_CONTENT):
        yield
    clear_cache()


class TestLoadContentFile:
    # Force the DB path to fail so tests deterministically exercise the file fallback.
    db_unavailable = patch("transcribe_api.runtime.db_connection.get_engine", side_effect=Exception("db unavailable"))

    def test_raises_when_file_missing(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = False

        with self.db_unavailable, patch("transcribe_api.documents.content_loader.CONTENT_FILE", mock_path), pytest.raises(ContentLoadError, match="not found"):
            _load_content_file()

    def test_raises_on_invalid_json(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = True

        with self.db_unavailable, patch("transcribe_api.documents.content_loader.CONTENT_FILE", mock_path), patch("json.load", side_effect=json.JSONDecodeError("bad", "doc", 0)), pytest.raises(ContentLoadError, match="Invalid JSON"):
            _load_content_file()

    def test_clear_cache_resets_lru(self):
        """clear_cache() should not raise (cache removed, now a no-op)."""
        clear_cache()  # Should not raise


class TestGetHearingTypes:
    def test_returns_list(self):
        result = get_hearing_types()
        assert isinstance(result, list)

    def test_returns_expected_count(self):
        assert len(get_hearing_types()) == 3

    def test_missing_hearing_types_returns_empty_list(self):
        with patch("transcribe_api.documents.content_loader.get_document_content", return_value={}):
            assert get_hearing_types() == []


class TestGetHearingTypeById:
    def test_known_id_returns_dict(self):
        result = get_hearing_type_by_id("face_to_face")
        assert result is not None
        assert result["id"] == "face_to_face"
        assert result["title"] == "Face to face"

    def test_unknown_id_returns_none(self):
        assert get_hearing_type_by_id("nonexistent") is None

    def test_empty_string_returns_none(self):
        assert get_hearing_type_by_id("") is None


class TestGetHearingTypeByTitle:
    def test_exact_match_returns_dict(self):
        result = get_hearing_type_by_title("Face to face")
        assert result is not None
        assert result["id"] == "face_to_face"

    def test_case_insensitive_match(self):
        result = get_hearing_type_by_title("FACE TO FACE")
        assert result is not None
        assert result["id"] == "face_to_face"

    def test_whitespace_stripped(self):
        result = get_hearing_type_by_title("  Video  ")
        assert result is not None
        assert result["id"] == "video"

    def test_unknown_title_returns_none(self):
        assert get_hearing_type_by_title("Unknown type") is None


class TestGetLegalFrameworks:
    def test_returns_list(self):
        assert isinstance(get_legal_frameworks(), list)

    def test_returns_expected_count(self):
        assert len(get_legal_frameworks()) == 2

    def test_missing_key_returns_empty_list(self):
        with patch("transcribe_api.documents.content_loader.get_document_content", return_value={}):
            assert get_legal_frameworks() == []


class TestGetLegalFrameworkById:
    def test_known_id_returns_framework(self):
        result = get_legal_framework_by_id("asylum_pre_naba")
        assert result is not None
        assert result["title"] == "Asylum Pre-NABA"

    def test_unknown_id_returns_none(self):
        assert get_legal_framework_by_id("missing") is None


class TestGetLegalFrameworkByTitle:
    def test_exact_match(self):
        result = get_legal_framework_by_title("EEA")
        assert result is not None
        assert result["id"] == "eea"

    def test_case_insensitive(self):
        result = get_legal_framework_by_title("eea")
        assert result is not None
        assert result["id"] == "eea"

    def test_unknown_title_returns_none(self):
        assert get_legal_framework_by_title("No match") is None


class TestGetAnonymityStatuses:
    def test_returns_list(self):
        assert isinstance(get_anonymity_statuses(), list)

    def test_returns_correct_count(self):
        assert len(get_anonymity_statuses()) == 2

    def test_empty_anonymity_section_returns_empty_list(self):
        with patch("transcribe_api.documents.content_loader.get_document_content", return_value={"anonymity": {}}):
            assert get_anonymity_statuses() == []

    def test_missing_anonymity_key_returns_empty_list(self):
        with patch("transcribe_api.documents.content_loader.get_document_content", return_value={}):
            assert get_anonymity_statuses() == []


class TestGetAnonymityStatusById:
    def test_known_status_returned(self):
        result = get_anonymity_status_by_id("granted")
        assert result is not None
        assert result["id"] == "granted"
        assert result["identifier"] == "Yes"

    def test_unknown_status_returns_none(self):
        assert get_anonymity_status_by_id("unknown") is None


class TestGetHearingDescription:
    def test_by_id(self):
        result = get_hearing_description("face_to_face")
        assert "face to face" in result.lower()

    def test_by_title(self):
        result = get_hearing_description("Face to face")
        assert "face to face" in result.lower()

    def test_unknown_returns_placeholder(self):
        result = get_hearing_description("totally_unknown_type")
        assert "totally_unknown_type" in result

    def test_found_but_missing_description_field_returns_placeholder(self):
        content = {
            "hearing_types": [{"id": "test", "title": "Test", "hearing_title": "Test"}],
            "legal_frameworks": [],
            "anonymity": {"statuses": []},
        }
        with patch("transcribe_api.documents.content_loader.get_document_content", return_value=content):
            result = get_hearing_description("test")
            assert "[test]" in result


class TestGetHearingTitle:
    def test_by_id(self):
        result = get_hearing_title("face_to_face")
        assert result == "The Hearing"

    def test_by_title_for_different_hearing(self):
        result = get_hearing_title("without_hearing")
        assert result == "Proceeding Without A Hearing"

    def test_unknown_type_returns_default(self):
        assert get_hearing_title("unknown") == "The Hearing"


class TestGetLegalFrameworkContent:
    def test_empty_list_returns_placeholder(self):
        assert get_legal_framework_content([]) == "[No legal frameworks selected]"

    def test_single_framework_by_id(self):
        result = get_legal_framework_content(["asylum_pre_naba"])
        assert "Asylum pre-NABA content." in result

    def test_multiple_frameworks_joined(self):
        result = get_legal_framework_content(["asylum_pre_naba", "eea"])
        assert "Asylum pre-NABA content." in result
        assert "EEA content." in result
        assert "\n\n" in result

    def test_unknown_framework_omitted(self):
        result = get_legal_framework_content(["asylum_pre_naba", "nonexistent"])
        assert "Asylum pre-NABA content." in result
        assert "nonexistent" not in result

    def test_all_unknown_returns_placeholder(self):
        result = get_legal_framework_content(["bad1", "bad2"])
        assert result == "[Legal frameworks not found]"

    def test_lookup_by_title_works(self):
        result = get_legal_framework_content(["Asylum Pre-NABA"])
        assert "Asylum pre-NABA content." in result


class TestGetLegalFrameworkIssues:
    def test_empty_list_returns_empty(self):
        assert get_legal_framework_issues([]) == []

    def test_single_framework_returns_its_issues(self):
        result = get_legal_framework_issues(["asylum_pre_naba"])
        assert "Refugee Convention issue" in result
        assert "Human rights issue" in result

    def test_multiple_frameworks_combined(self):
        result = get_legal_framework_issues(["asylum_pre_naba", "eea"])
        assert len(result) == 3

    def test_unknown_framework_skipped(self):
        result = get_legal_framework_issues(["nonexistent"])
        assert result == []

    def test_framework_with_no_issues_is_silently_skipped(self):
        content = {
            **SAMPLE_CONTENT,
            "legal_frameworks": [
                *SAMPLE_CONTENT["legal_frameworks"],
                {"id": "credibility", "title": "Credibility", "rank": 3, "content": "Credibility content.", "issues": []},
            ],
        }
        with patch("transcribe_api.documents.content_loader.get_document_content", return_value=content):
            result = get_legal_framework_issues(["asylum_pre_naba", "credibility"])
        assert "Refugee Convention issue" in result
        assert "Credibility" not in result


class TestGetLegalIssuesDisplay:
    def test_empty_list_returns_placeholder(self):
        assert get_legal_issues_display([]) == "[No legal issues selected]"

    def test_single_framework_returns_enumerated_issues(self):
        result = get_legal_issues_display(["asylum_pre_naba"])
        assert result == "(a) Refugee Convention issue\n(b) Human rights issue"

    def test_multiple_frameworks_enumerate_all_issues(self):
        result = get_legal_issues_display(["asylum_pre_naba", "eea"])
        assert result == "(a) Refugee Convention issue\n(b) Human rights issue\n(c) EEA issue A"

    def test_unknown_framework_returns_no_issues_placeholder(self):
        result = get_legal_issues_display(["nonexistent_id"])
        assert result == "[No legal issues selected]"

    def test_framework_with_no_issues_does_not_appear_in_display(self):
        content = {
            **SAMPLE_CONTENT,
            "legal_frameworks": [
                *SAMPLE_CONTENT["legal_frameworks"],
                {"id": "credibility", "title": "Credibility", "rank": 3, "content": "Credibility content.", "issues": []},
            ],
        }
        with patch("transcribe_api.documents.content_loader.get_document_content", return_value=content):
            result = get_legal_issues_display(["asylum_pre_naba", "credibility"])
        assert "Credibility" not in result
        assert result == "(a) Refugee Convention issue\n(b) Human rights issue"
