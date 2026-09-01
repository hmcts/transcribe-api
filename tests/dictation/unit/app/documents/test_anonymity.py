"""Unit tests for anonymity content logic."""

from unittest.mock import patch

import pytest

from transcribe_api.documents.anonymity import (
    DELETE_MARKER,
    AnonymityContent,
    AnonymityStatus,
    get_anonymity_content,
    parse_anonymity_status,
)


class TestParseAnonymityStatus:
    def test_none_returns_not_sought(self):
        assert parse_anonymity_status(None) == AnonymityStatus.NOT_SOUGHT

    def test_empty_string_returns_not_sought(self):
        assert parse_anonymity_status("") == AnonymityStatus.NOT_SOUGHT

    def test_whitespace_only_returns_not_sought(self):
        assert parse_anonymity_status("   ") == AnonymityStatus.NOT_SOUGHT

    @pytest.mark.parametrize("value", ["granted", "yes", "true", "1", "GRANTED", "YES", "True"])
    def test_granted_variants(self, value):
        assert parse_anonymity_status(value) == AnonymityStatus.GRANTED

    @pytest.mark.parametrize("value", [
        "sought_but_refused", "refused", "one was sought but refused",
        "SOUGHT_BUT_REFUSED", "REFUSED",
    ])
    def test_sought_but_refused_variants(self, value):
        assert parse_anonymity_status(value) == AnonymityStatus.SOUGHT_BUT_REFUSED

    @pytest.mark.parametrize("value", ["not_sought", "no", "none", "false", "0", "NOT_SOUGHT"])
    def test_not_sought_variants(self, value):
        assert parse_anonymity_status(value) == AnonymityStatus.NOT_SOUGHT

    def test_unknown_value_defaults_to_not_sought(self):
        assert parse_anonymity_status("something_random") == AnonymityStatus.NOT_SOUGHT

    def test_strips_whitespace_before_matching(self):
        assert parse_anonymity_status("  granted  ") == AnonymityStatus.GRANTED
        assert parse_anonymity_status("  refused  ") == AnonymityStatus.SOUGHT_BUT_REFUSED


class TestAnonymityContentToTemplateDict:
    def _make_content(self, **kwargs):
        defaults = {
            "reasons_text": "Some reasons",
            "order_identifier": "Yes",
            "head": "(ANONYMITY ORDER MADE)",
            "reasons_heading": "Reasons Heading",
            "order_text": "Order text",
        }
        defaults.update(kwargs)
        return AnonymityContent(**defaults)

    def test_order_identifier_prefixed_when_present(self):
        content = self._make_content(order_identifier="Yes")
        d = content.to_template_dict()
        assert d["AnonymityOrder"] == "Anonymity order made: Yes"

    def test_delete_marker_not_prefixed(self):
        content = self._make_content(order_identifier=DELETE_MARKER)
        d = content.to_template_dict()
        assert d["AnonymityOrder"] == DELETE_MARKER

    def test_empty_order_identifier_not_prefixed(self):
        content = self._make_content(order_identifier="")
        d = content.to_template_dict()
        assert d["AnonymityOrder"] == ""

    def test_all_keys_present(self):
        content = self._make_content()
        d = content.to_template_dict()
        assert set(d.keys()) == {
            "AnonymityHead",
            "AnonymityReasonsHeading",
            "AnonymityReasonsText",
            "AnonymityOrder",
            "AnonymityOrderText",
        }

    def test_values_mapped_correctly(self):
        content = self._make_content(
            reasons_text="My reasons",
            head="My head",
            reasons_heading="My heading",
            order_text="My order text",
        )
        d = content.to_template_dict()
        assert d["AnonymityReasonsText"] == "My reasons"
        assert d["AnonymityHead"] == "My head"
        assert d["AnonymityReasonsHeading"] == "My heading"
        assert d["AnonymityOrderText"] == "My order text"


class TestAnonymityContentGetDeleteControls:
    def test_no_delete_markers(self):
        content = AnonymityContent(
            reasons_text="text",
            order_identifier="Yes",
            head="head",
            reasons_heading="heading",
            order_text="order",
        )
        assert content.get_delete_controls() == set()

    def test_single_delete_marker(self):
        content = AnonymityContent(
            reasons_text=DELETE_MARKER,
            order_identifier="Yes",
            head="head",
            reasons_heading="heading",
            order_text="order",
        )
        assert "AnonymityReasonsText" in content.get_delete_controls()

    def test_multiple_delete_markers(self):
        content = AnonymityContent(
            reasons_text=DELETE_MARKER,
            order_identifier=DELETE_MARKER,
            head=DELETE_MARKER,
            reasons_heading="heading",
            order_text="order",
        )
        controls = content.get_delete_controls()
        assert "AnonymityReasonsText" in controls
        assert "AnonymityHead" in controls
        # AnonymityOrder value is DELETE_MARKER because identifier is DELETE_MARKER
        assert "AnonymityOrder" in controls

    def test_all_delete_markers(self):
        content = AnonymityContent(
            reasons_text=DELETE_MARKER,
            order_identifier=DELETE_MARKER,
            head=DELETE_MARKER,
            reasons_heading=DELETE_MARKER,
            order_text=DELETE_MARKER,
        )
        assert len(content.get_delete_controls()) == 5


class TestGetAnonymityContent:
    def _mock_status_data(self, status_value: str):
        return {
            "content": f"Reasons for {status_value}",
            "identifier": status_value,
            "header": f"Header for {status_value}",
            "order_header": f"Order header for {status_value}",
            "order_text": f"Order text for {status_value}",
        }

    def test_returns_anonymity_content_for_granted(self):
        with patch("transcribe_api.documents.anonymity.get_anonymity_status_by_id") as mock_get:
            mock_get.return_value = self._mock_status_data("granted")

            result = get_anonymity_content(AnonymityStatus.GRANTED)

        assert isinstance(result, AnonymityContent)
        assert result.order_identifier == "granted"

    def test_raises_value_error_for_missing_status(self):
        with patch("transcribe_api.documents.anonymity.get_anonymity_status_by_id", return_value=None), pytest.raises(ValueError, match="not found in configuration"):
            get_anonymity_content(AnonymityStatus.NOT_SOUGHT)

    def test_missing_fields_default_to_delete_marker(self):
        with patch("transcribe_api.documents.anonymity.get_anonymity_status_by_id", return_value={}):

            result = get_anonymity_content(AnonymityStatus.NOT_SOUGHT)

        assert result.reasons_text == DELETE_MARKER
        assert result.head == DELETE_MARKER
        assert result.reasons_heading == DELETE_MARKER
        assert result.order_text == DELETE_MARKER
        assert result.order_identifier == ""
