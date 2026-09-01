"""Unit tests for templates metadata."""

from transcribe_api.documents.minutes.templates.templates_metadata import (
    all_templates,
    get_all_templates,
)


class TestGetAllTemplates:
    def test_returns_list(self):
        result = get_all_templates()
        assert isinstance(result, list)

    def test_contains_general_and_crissa(self):
        result = get_all_templates()
        names = [t.name for t in result]
        assert "General" in names
        assert "Crissa" in names

    def test_returns_same_list_as_all_templates(self):
        assert get_all_templates() is all_templates
