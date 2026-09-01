"""Tests for lib.blocks helper functions."""

from transcribe_api.documents.lib.blocks import (
    enumerated_list_blocks,
    nested_list_blocks_from_newline_text,
    paragraph_blocks,
    section_heading_blocks,
)


def test_paragraph_blocks_normalize_soft_line_breaks_and_default_to_justify() -> None:
    blocks = paragraph_blocks("First line\nstill first para\n\nSecond para")

    assert len(blocks) == 2
    assert blocks[0].text == "First line still first para"
    assert blocks[0].alignment == "justify"
    assert blocks[1].text == "Second para"


def test_enumerated_list_blocks_use_word_numbering_levels() -> None:
    numeric = enumerated_list_blocks(["One", "Two"], style="numeric")
    alphabet = enumerated_list_blocks(["Alpha", "Beta"], style="alphabet")

    assert [b.text for b in numeric] == ["One", "Two"]
    assert [b.text for b in alphabet] == ["Alpha", "Beta"]
    assert all(b.list_level == 0 for b in numeric)
    assert all(b.list_level == 1 for b in alphabet)
    assert all(b.left_indent_pt is None for b in numeric)
    assert all(b.first_line_indent_pt is None for b in numeric)


def test_section_heading_blocks_apply_bold_and_underline_switches() -> None:
    headings = section_heading_blocks(["Heading A", "Heading B"], bold=True, underline=True)

    assert len(headings) == 2
    assert headings[0].text == "Heading A"
    assert headings[0].bold is True
    assert headings[0].underline is True


def test_nested_list_blocks_from_newline_text_builds_nested_levels() -> None:
    content = "Intro paragraph\n(1) First item\n(a) Nested alpha\n(2) Second item\n"

    blocks = nested_list_blocks_from_newline_text(content)

    assert blocks[0].text == "Intro paragraph"
    assert blocks[0].list_level == 0
    assert blocks[1].text == "First item"
    assert blocks[1].list_level == 0
    assert blocks[2].text == "Nested alpha"
    assert blocks[2].list_level == 1
    assert blocks[3].text == "Second item"
    assert blocks[3].list_level == 0
