"""Tests for Markdown-to-block conversion helpers."""

from transcribe_api.documents.lib.docx_style_profile import INDENT_INCREMENT_CM, STYLE_NUM_NORMAL, STYLE_QUOTATION, cm_to_pt
from transcribe_api.documents.lib.markdown_to_blocks import markdown_to_blocks, markdown_to_plain_text
from transcribe_api.documents.lib.models import Heading, Paragraph


def test_markdown_to_blocks_parses_headings_and_inline_formatting() -> None:
    blocks = markdown_to_blocks("# Title\n\nA **bold** and *italic* paragraph.")

    assert len(blocks) == 2
    assert isinstance(blocks[0], Heading)
    assert blocks[0].text == "Title"
    assert blocks[0].level == 1
    assert blocks[0].underline is True

    assert isinstance(blocks[1], Paragraph)
    assert blocks[1].text == "A bold and italic paragraph."
    assert blocks[1].bold is True
    assert blocks[1].italic is True
    assert blocks[1].alignment == "justify"


def test_markdown_to_blocks_sets_list_levels_for_nested_lists() -> None:
    blocks = markdown_to_blocks("1. First\n   1. Nested\n2. Second")

    assert [block.text for block in blocks if isinstance(block, Paragraph)] == ["First", "Nested", "Second"]
    assert [block.list_level for block in blocks if isinstance(block, Paragraph)] == [1, 2, 1]
    assert all(block.style == STYLE_NUM_NORMAL for block in blocks if isinstance(block, Paragraph))


def test_markdown_to_blocks_keeps_unordered_lists_at_base_level() -> None:
    blocks = markdown_to_blocks("- First\n  - Nested\n- Second")

    assert [block.text for block in blocks if isinstance(block, Paragraph)] == ["First", "Nested", "Second"]
    assert [block.list_level for block in blocks if isinstance(block, Paragraph)] == [0, 1, 0]
    assert all(block.style == STYLE_NUM_NORMAL for block in blocks if isinstance(block, Paragraph))


def test_markdown_to_blocks_styles_quotes_and_code_blocks() -> None:
    blocks = markdown_to_blocks("> Quoted text\n\n```python\nprint('ok')\n```")
    quote_indent = cm_to_pt(INDENT_INCREMENT_CM) * 2

    assert len(blocks) == 2

    quote_block = blocks[0]
    assert isinstance(quote_block, Paragraph)
    assert quote_block.text == "Quoted text"
    assert quote_block.style == STYLE_QUOTATION
    assert quote_block.italic is True
    assert quote_block.alignment == "justify"
    assert quote_block.left_indent_pt == quote_indent

    code_block = blocks[1]
    assert isinstance(code_block, Paragraph)
    assert code_block.text == "print('ok')"
    assert code_block.style == STYLE_QUOTATION
    assert code_block.alignment == "left"
    assert code_block.left_indent_pt == quote_indent


def test_markdown_to_blocks_list_inside_blockquote_is_italic() -> None:
    """Ordered list items inside a blockquote render as STYLE_QUOTATION with an italic
    alpha label prepended as literal text, so the label inherits italic formatting.
    Word's STYLE_NUM_NORMAL auto-generates labels outside the run and cannot be made italic."""
    md = "> Preamble text\n>\n> 1. First item\n> 2. Second item"
    blocks = markdown_to_blocks(md)

    paragraphs = [b for b in blocks if isinstance(b, Paragraph)]
    assert len(paragraphs) == 3

    preamble = paragraphs[0]
    assert preamble.text == "Preamble text"
    assert preamble.style == STYLE_QUOTATION
    assert preamble.italic is True

    first, second = paragraphs[1], paragraphs[2]
    assert first.style == STYLE_QUOTATION, "ordered item in blockquote must use STYLE_QUOTATION"
    assert first.italic is True
    assert first.text.startswith("(a)\t"), "first item must carry '(a)' label as literal text"

    assert second.style == STYLE_QUOTATION
    assert second.italic is True
    assert second.text.startswith("(b)\t"), "second item must carry '(b)' label as literal text"


def test_markdown_to_plain_text_preserves_block_separation() -> None:
    text = markdown_to_plain_text("# Header\n\n- One\n- Two\n\n> Quote\n\n`code` and **bold**")

    assert text == "Header\n\nOne\n\nTwo\n\nQuote\n\ncode and bold"
