"""Convert Markdown text into structured Word document blocks.

Uses the ``mistletoe`` parser to build an AST and visitor classes to map
tokens into ``Paragraph`` / ``Heading`` blocks for the document renderer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from functools import singledispatchmethod
from typing import Literal

from mistletoe import Document as MarkdownDocument
from mistletoe import block_token as md_block
from mistletoe import span_token as md_span

from transcribe_api.documents.lib.docx_style_profile import INDENT_INCREMENT_CM, STYLE_NUM_NORMAL, STYLE_QUOTATION, cm_to_pt
from transcribe_api.documents.lib.models import ContentBlock, Heading, Paragraph

Alignment = Literal["left", "center", "right", "justify"]
MarkdownDocumentToken = md_block.Document
MarkdownHeadingToken = md_block.Heading
MarkdownSetextHeadingToken = md_block.SetextHeading
MarkdownParagraphToken = md_block.Paragraph
MarkdownQuoteToken = md_block.Quote
MarkdownListToken = md_block.List
MarkdownListItemToken = md_block.ListItem
CodeFence = md_block.CodeFence
BlockCode = md_block.BlockCode
ThematicBreak = md_block.ThematicBreak
RawText = md_span.RawText
LineBreak = md_span.LineBreak
Strong = md_span.Strong
Emphasis = md_span.Emphasis

INDENT_INCREMENT_PT = cm_to_pt(INDENT_INCREMENT_CM)
_MIN_QUOTE_INDENT_LEVEL = 2


def _parse_markdown(content: str) -> MarkdownDocumentToken:
    return MarkdownDocument(content)


def _iter_children(token: object) -> tuple[object, ...]:
    children = getattr(token, "children", None)
    if children is None:
        return ()
    return tuple(children)


def _collect_text(token: object) -> str:
    if isinstance(token, RawText):
        return token.content
    if isinstance(token, LineBreak):
        return " " if token.soft else "\n"

    children = _iter_children(token)
    if children:
        return "".join(_collect_text(child) for child in children)

    content = getattr(token, "content", None)
    return content if isinstance(content, str) else ""


_UNDERLINE_TAG_RE = re.compile(r"</?u>", re.IGNORECASE)


def _strip_underline(text: str) -> tuple[str, bool]:
    """Remove <u> and </u> tags and report whether any were present."""
    has_underline = bool(_UNDERLINE_TAG_RE.search(text))
    return _UNDERLINE_TAG_RE.sub("", text), has_underline


def _detect_inline_formatting(token: object) -> tuple[bool, bool]:
    has_bold = isinstance(token, Strong)
    has_italic = isinstance(token, Emphasis)

    for child in _iter_children(token):
        child_bold, child_italic = _detect_inline_formatting(child)
        has_bold = has_bold or child_bold
        has_italic = has_italic or child_italic
        if has_bold and has_italic:
            break
    return has_bold, has_italic


@dataclass(frozen=True, slots=True)
class _ParseContext:
    alignment: Alignment
    quote_depth: int = 0
    list_level: int = -1
    list_item_label: str | None = None  # pre-computed "(a)" etc. for ordered items in blockquotes

    def enter_quote(self) -> _ParseContext:
        return replace(self, quote_depth=self.quote_depth + 1)

    def enter_list(self, *, ordered: bool) -> _ParseContext:
        # NumNormal level 0 is decimal. Ordered lists start at level 1 (a),
        # then level 2 (i). Unordered lists keep the existing base level.
        next_level = (1 if ordered else 0) if self.list_level < 0 else self.list_level + 1
        return replace(self, list_level=next_level, list_item_label=None)


class _MarkdownBlockVisitor:
    """Visitor that maps mistletoe block tokens to ContentBlock values."""

    def __init__(self, *, alignment: Alignment) -> None:
        self._root_context = _ParseContext(alignment=alignment)
        self._blocks: list[ContentBlock] = []

    def collect(self, root: MarkdownDocumentToken) -> list[ContentBlock]:
        self._visit_children(root, self._root_context)
        return self._blocks

    def _visit_children(self, token: object, context: _ParseContext) -> None:
        for child in _iter_children(token):
            self.visit(child, context)

    @singledispatchmethod
    def visit(self, token: object, context: _ParseContext) -> None:
        self._visit_children(token, context)

    @visit.register
    def _visit_paragraph(self, token: MarkdownParagraphToken, context: _ParseContext) -> None:
        raw = _collect_text(token).strip()
        if not raw:
            return
        text, underline = _strip_underline(raw)
        bold, italic = _detect_inline_formatting(token)
        self._emit_paragraph(text=text, context=context, bold=bold, italic=italic, underline=underline)

    @visit.register
    def _visit_heading(self, token: MarkdownHeadingToken, _context: _ParseContext) -> None:
        self._emit_heading(token.level, _collect_text(token).strip())

    @visit.register
    def _visit_setext_heading(self, token: MarkdownSetextHeadingToken, _context: _ParseContext) -> None:
        self._emit_heading(token.level, _collect_text(token).strip())

    @visit.register
    def _visit_quote(self, token: MarkdownQuoteToken, context: _ParseContext) -> None:
        self._visit_children(token, context.enter_quote())

    @visit.register
    def _visit_list(self, token: MarkdownListToken, context: _ParseContext) -> None:
        is_ordered = getattr(token, "start", None) is not None
        child_context = context.enter_list(ordered=is_ordered)
        if is_ordered and context.quote_depth > 0:
            # Pre-compute "(a)", "(b)" labels so _emit_paragraph can render them as italic text
            # rather than relying on Word's numbering definition (which ignores run-level italic).
            start = getattr(token, "start", 1) or 1
            for i, child in enumerate(_iter_children(token)):
                label = f"({chr(ord('a') + start + i - 1)})"
                self.visit(child, replace(child_context, list_item_label=label))
        else:
            self._visit_children(token, child_context)

    @visit.register
    def _visit_list_item(self, token: MarkdownListItemToken, context: _ParseContext) -> None:
        self._visit_children(token, context)

    @visit.register
    def _visit_thematic_break(self, _token: ThematicBreak, _context: _ParseContext) -> None:
        return

    @visit.register
    def _visit_code_fence(self, token: CodeFence, _context: _ParseContext) -> None:
        self._emit_code_block(token.content)

    @visit.register
    def _visit_block_code(self, token: BlockCode, _context: _ParseContext) -> None:
        self._emit_code_block(token.content)

    def _emit_heading(self, level: int, text: str) -> None:
        if not text:
            return
        self._blocks.append(
            Heading(
                text=text,
                level=level,
                bold=True,
                underline=level <= 2,  # noqa: PLR2004
            )
        )

    def _emit_paragraph(self, *, text: str, context: _ParseContext, bold: bool, italic: bool, underline: bool = False) -> None:
        if context.list_level >= 0 and context.quote_depth > 0:
            indent_pt = INDENT_INCREMENT_PT * max(context.quote_depth + 1, _MIN_QUOTE_INDENT_LEVEL)
            if context.list_item_label is not None:
                # Ordered list item inside a blockquote — render as STYLE_QUOTATION with the
                # alpha label as literal text so it inherits italic. Word's numbering definition
                # does not apply run-level italic to the auto-generated label.
                self._blocks.append(
                    Paragraph(
                        text=f"{context.list_item_label}\t{text}",
                        style=STYLE_QUOTATION,
                        italic=True,
                        alignment=context.alignment,
                        left_indent_pt=indent_pt,
                        bold=bold,
                        underline=underline,
                    )
                )
            else:
                # Unordered list item inside a blockquote — keep Word list numbering.
                self._blocks.append(
                    Paragraph(
                        text=text,
                        style=STYLE_NUM_NORMAL,
                        alignment=context.alignment,
                        list_level=context.list_level,
                        bold=bold,
                        italic=True,
                        underline=underline,
                    )
                )
            return

        if context.list_level >= 0:
            self._blocks.append(
                Paragraph(
                    text=text,
                    style=STYLE_NUM_NORMAL,
                    alignment=context.alignment,
                    list_level=context.list_level,
                    bold=bold,
                    italic=italic,
                    underline=underline,
                )
            )
            return

        if context.quote_depth > 0:
            indent_pt = INDENT_INCREMENT_PT * max(context.quote_depth + 1, _MIN_QUOTE_INDENT_LEVEL)
            self._blocks.append(
                Paragraph(
                    text=text,
                    style=STYLE_QUOTATION,
                    italic=True,
                    alignment=context.alignment,
                    left_indent_pt=indent_pt,
                    underline=underline,
                )
            )
            return

        self._blocks.append(
            Paragraph(
                text=text,
                alignment=context.alignment,
                bold=bold,
                italic=italic,
                underline=underline,
            )
        )

    def _emit_code_block(self, code: str) -> None:
        text = code.strip()
        if not text:
            return
        self._blocks.append(
            Paragraph(
                text=text,
                style=STYLE_QUOTATION,
                alignment="left",
                left_indent_pt=INDENT_INCREMENT_PT * _MIN_QUOTE_INDENT_LEVEL,
            )
        )


class _PlainTextVisitor:
    """Collect plain-text fragments from mistletoe tokens."""

    def __init__(self) -> None:
        self._texts: list[str] = []

    def collect(self, root: MarkdownDocumentToken) -> list[str]:
        self.visit(root)
        return self._texts

    def _visit_children(self, token: object) -> None:
        for child in _iter_children(token):
            self.visit(child)

    @singledispatchmethod
    def visit(self, token: object) -> None:
        text = _collect_text(token).strip()
        if text:
            self._texts.append(text)

    @visit.register
    def _visit_document(self, token: MarkdownDocumentToken) -> None:
        self._visit_children(token)

    @visit.register
    def _visit_paragraph(self, token: MarkdownParagraphToken) -> None:
        text = _collect_text(token).strip()
        if text:
            self._texts.append(text)

    @visit.register
    def _visit_list(self, token: MarkdownListToken) -> None:
        self._visit_children(token)

    @visit.register
    def _visit_list_item(self, token: MarkdownListItemToken) -> None:
        self._visit_children(token)

    @visit.register
    def _visit_quote(self, token: MarkdownQuoteToken) -> None:
        self._visit_children(token)


def markdown_to_blocks(
    content: str,
    *,
    alignment: Alignment = "justify",
) -> list[ContentBlock]:
    """Parse Markdown text and return a flat list of document blocks."""
    if not content or not content.strip():
        return []

    ast = _parse_markdown(content)
    return _MarkdownBlockVisitor(alignment=alignment).collect(ast)


def _collect_block_texts(node: MarkdownDocumentToken) -> list[str]:
    """Collect text from each leaf-level block, one entry per paragraph block."""
    return _PlainTextVisitor().collect(node)


def markdown_to_plain_text(content: str) -> str:
    """Strip Markdown formatting and return plain text."""
    if not content or not content.strip():
        return content or ""

    ast = _parse_markdown(content)
    return "\n\n".join(_collect_block_texts(ast))
