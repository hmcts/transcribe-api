"""Helpers to build structured HearingDocument content blocks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from transcribe_api.documents.lib.docx_style_profile import INDENT_INCREMENT_CM, STYLE_NUM_NORMAL, STYLE_QUOTATION, cm_to_pt
from transcribe_api.documents.lib.models import Heading, Paragraph

INDENT_INCREMENT_PT = cm_to_pt(INDENT_INCREMENT_CM)


@dataclass(slots=True)
class _ListTreeNode:
    text: str
    level: int
    is_list_item: bool
    children: list[_ListTreeNode] = field(default_factory=list)


_NUMERIC_MARKER_RE = re.compile(r"^(?:\((?P<num_paren>\d+)\)|(?P<num_dot>\d+)\.)\s+")
_ALPHA_MARKER_RE = re.compile(r"^(?:\((?P<alpha_paren>[a-z])\)|(?P<alpha_dot>[a-z])[.)])\s+", re.IGNORECASE)
_ROMAN_MARKER_RE = re.compile(r"^(?:\((?P<roman_paren>[ivxlcdm]+)\)|(?P<roman_dot>[ivxlcdm]+)[.)])\s+", re.IGNORECASE)
_BULLET_RE = re.compile(r"^[-\u2022]\s+")


def _normalize_text_inputs(content: str | list[str]) -> list[str]:
    if isinstance(content, str):
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        # Split paragraphs on blank lines, then flatten soft line breaks.
        chunks = [chunk.strip() for chunk in normalized.split("\n\n")]
        return [chunk.replace("\n", " ") for chunk in chunks if chunk.strip()]
    return [item.strip() for item in content if item.strip()]


def _line_level(line: str) -> tuple[int, bool, str]:
    """Detect a list-marker prefix and return ``(level, is_list_item, text)``.

    The returned *text* is the content only — markers are stripped because
    Word's numbering definition renders them automatically via ``w:numPr``.

    Level mapping to numbering definition ilvl: ``level - 1``.
    """
    stripped = line.strip()

    numeric_match = _NUMERIC_MARKER_RE.match(stripped)
    if numeric_match:
        return 1, True, stripped[numeric_match.end() :].strip()

    alpha_match = _ALPHA_MARKER_RE.match(stripped)
    if alpha_match:
        return 2, True, stripped[alpha_match.end() :].strip()

    roman_match = _ROMAN_MARKER_RE.match(stripped)
    if roman_match:
        return 3, True, stripped[roman_match.end() :].strip()

    bullet_match = _BULLET_RE.match(stripped)
    if bullet_match:
        return 2, True, stripped[bullet_match.end() :].strip()

    return 0, False, stripped


def _flatten_list_tree(nodes: list[_ListTreeNode]) -> list[Paragraph]:
    blocks: list[Paragraph] = []

    def walk(node: _ListTreeNode) -> None:
        ilvl = max(node.level - 1, 0)
        blocks.append(
            Paragraph(
                text=node.text,
                alignment="justify",
                style=STYLE_NUM_NORMAL,
                list_level=ilvl,
            )
        )
        for child in node.children:
            walk(child)

    for node in nodes:
        walk(node)
    return blocks


def nested_list_blocks_from_newline_text(content: str) -> list[Paragraph]:
    """Parse newline-separated text into paragraph + nested list blocks.

    The parser inspects line prefixes after each ``\\n`` and builds a simple
    tree of list hierarchy before flattening to indented Paragraph blocks.
    """

    lines = [line.strip() for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
    if not lines:
        return []

    roots: list[_ListTreeNode] = []
    stack: list[_ListTreeNode] = []

    for line in lines:
        level, is_list_item, normalized_text = _line_level(line)
        node = _ListTreeNode(text=normalized_text, level=level, is_list_item=is_list_item)

        if not is_list_item:
            stack.clear()
            roots.append(node)
            continue

        while stack and stack[-1].level >= level:
            stack.pop()

        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    return _flatten_list_tree(roots)


def paragraph_blocks(
    content: str | list[str],
    *,
    alignment: Literal["left", "center", "right", "justify"] = "justify",
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
    font_size: int | None = None,
) -> list[Paragraph]:
    """Create paragraph blocks from string or string-array input.

    Defaults to ``justify`` alignment and removes soft line breaks within each
    paragraph so Word can render normal full-width justification while keeping
    the final line naturally ragged (not stretched).
    """

    return [
        Paragraph(
            text=text,
            alignment=alignment,
            bold=bold,
            italic=italic,
            underline=underline,
            font_size=font_size,
        )
        for text in _normalize_text_inputs(content)
    ]


def enumerated_list_blocks(
    items: str | list[str],
    *,
    style: Literal["numeric", "alphabet"] = "numeric",
    alignment: Literal["left", "center", "right", "justify"] = "justify",
) -> list[Paragraph]:
    """Create enumerated list blocks using Word's native numbering.

    The NumNormal style carries a ``w:numPr`` that activates a 9-level
    numbering definition.  ``list_level`` selects the tier:

    * ``numeric`` → ilvl 0  (``1.``, ``2.``, …)
    * ``alphabet`` → ilvl 1  (``(a)``, ``(b)``, …)

    Word renders the marker automatically; the text contains only content.
    """
    list_level = 0 if style == "numeric" else 1
    return [
        Paragraph(
            text=item,
            style=STYLE_NUM_NORMAL,
            alignment=alignment,
            list_level=list_level,
        )
        for item in _normalize_text_inputs(items)
    ]


def section_heading_blocks(
    headings: str | list[str],
    *,
    level: int = 1,
    bold: bool = True,
    underline: bool = True,
    alignment: Literal["left", "center", "right", "justify"] = "left",
) -> list[Heading]:
    """Create section heading blocks with configurable bold/underline."""

    return [
        Heading(
            text=text,
            level=level,
            bold=bold,
            underline=underline,
            alignment=alignment,
        )
        for text in _normalize_text_inputs(headings)
    ]


def quotation_blocks(content: str | list[str], *, indent_level: int = 2) -> list[Paragraph]:
    """Create quoted paragraphs in the dedicated quotation style."""
    left_indent = INDENT_INCREMENT_PT * max(indent_level, 2)
    return [
        Paragraph(
            text=text,
            style=STYLE_QUOTATION,
            italic=True,
            alignment="justify",
            left_indent_pt=left_indent,
        )
        for text in _normalize_text_inputs(content)
    ]
