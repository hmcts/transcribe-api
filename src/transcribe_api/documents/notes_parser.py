"""Parser for TipTap HTML editor content into structured document blocks.

Converts the HTML produced by the TipTap notes editor into ``Paragraph``
blocks grouped by the three recognised section headings (Background,
Evidence, Facts).
"""

from __future__ import annotations

from html.parser import HTMLParser

from transcribe_api.documents.lib.docx_style_profile import (
    INDENT_INCREMENT_CM,
    STYLE_NUM_NORMAL,
    STYLE_QUOTATION,
    cm_to_pt,
)
from transcribe_api.documents.lib.models import Paragraph

_LIST_ITEM_ILVL = 1

# Map of section heading text (lower-cased) to the canonical section key.
_SECTION_ALIAS_MAP: dict[str, str] = {
    "background": "background",
    "evidence": "evidence",
    "facts": "facts",
    "finding of fact": "facts",
    "findings of fact": "facts",
}

_SECTION_KEYS = ("background", "evidence", "facts")


class _TipTapHTMLParser(HTMLParser):
    """Parse TipTap HTML into structured ``Paragraph`` blocks per section.

    Supported constructs
    --------------------
    * ``<h1>``-``<h6>`` whose text matches a section alias → section boundary
    * ``<p>`` (outside a list item) → ``Paragraph`` block
    * ``<ul><li>`` → bullet ``Paragraph`` (``•``) with hanging indent
    * ``<ol><li>`` → numbered ``Paragraph`` (``1.``, ``2.``…) with hanging indent
    * ``<strong>`` / ``<b>`` → sets ``bold=True`` on the enclosing block when
      at least one character of text was collected while the tag was open
    * ``<em>`` / ``<i>`` → sets ``italic=True`` similarly
    * All other tags are silently ignored; their text content is still captured.
    """

    def __init__(
        self,
        aliases: dict[str, str] | None = None,
        default_section: str | None = None,
    ) -> None:
        super().__init__()

        # Heading text (lower-cased) -> section. With a default section (an uploaded
        # template's), text before any section heading goes there, and another heading
        # stays in its section as a bold paragraph; without, both are dropped.
        self._aliases = _SECTION_ALIAS_MAP if aliases is None else aliases
        self._keep_other_headings = default_section is not None
        # Sections accumulate Paragraph blocks
        keys = _SECTION_KEYS if aliases is None else dict.fromkeys(aliases.values())
        self._sections: dict[str, list[Paragraph]] = {k: [] for k in keys}
        self._current_section: str | None = default_section

        # --- per-block state ---
        self._in_heading: bool = False
        self._in_standalone_p: bool = False  # <p> outside <li>
        self._in_li: bool = False
        self._blockquote_depth: int = 0

        # Ordered-list state
        self._li_type: str | None = None  # "ul" or "ol"
        self._li_index: int = 0

        # Text accumulation
        self._text_parts: list[str] = []

        # Inline style tracking (depth counters so nested tags work)
        self._bold_depth: int = 0
        self._italic_depth: int = 0

        # Whether bold/italic was *active when text was collected* in this block
        self._block_has_bold: bool = False
        self._block_has_italic: bool = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reset_block(self) -> None:
        self._text_parts = []
        self._bold_depth = 0
        self._italic_depth = 0
        self._block_has_bold = False
        self._block_has_italic = False

    def _flush_paragraph(self, *, is_bullet: bool = False, is_ordered: bool = False) -> None:
        """Finalise accumulated text as a Paragraph and append to current section."""
        if self._current_section is None:
            return
        text = "".join(self._text_parts).strip()
        if not text:
            return

        quote_indent_level = min(max(self._blockquote_depth + 1, 2), 3)
        quote_indent_pt = cm_to_pt(INDENT_INCREMENT_CM * quote_indent_level)

        if self._blockquote_depth > 0:
            para = Paragraph(
                text=text,
                style=STYLE_QUOTATION,
                italic=True,
                left_indent_pt=quote_indent_pt,
            )
        elif is_bullet or is_ordered:
            para = Paragraph(
                text=text,
                style=STYLE_NUM_NORMAL,
                list_level=_LIST_ITEM_ILVL,
                bold=self._block_has_bold,
                italic=self._block_has_italic,
            )
        else:
            para = Paragraph(
                text=text,
                style=STYLE_NUM_NORMAL,
                alignment="justify",
                bold=self._block_has_bold,
                italic=self._block_has_italic,
            )

        self._sections[self._current_section].append(para)

    # ------------------------------------------------------------------
    # HTMLParser callbacks
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._reset_block()
            self._in_heading = True

        elif tag == "ul":
            self._li_type = "ul"
            self._li_index = 0

        elif tag == "ol":
            self._li_type = "ol"
            self._li_index = 0

        elif tag == "li":
            self._reset_block()
            self._li_index += 1
            self._in_li = True

        elif tag == "blockquote":
            self._blockquote_depth += 1

        elif tag == "p" and not self._in_li:
            self._reset_block()
            self._in_standalone_p = True

        elif tag in ("strong", "b"):
            self._bold_depth += 1

        elif tag in ("em", "i"):
            self._italic_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            heading_text = "".join(self._text_parts).strip()
            section = self._aliases.get(heading_text.lower())
            if section is not None or not self._keep_other_headings:
                self._current_section = section
            elif heading_text and self._current_section is not None:
                self._sections[self._current_section].append(Paragraph(text=heading_text, bold=True))
            self._in_heading = False
            self._reset_block()

        elif tag in ("ul", "ol"):
            self._li_type = None

        elif tag == "li":
            self._flush_paragraph(
                is_bullet=self._li_type == "ul",
                is_ordered=self._li_type == "ol",
            )
            self._in_li = False
            self._reset_block()

        elif tag == "blockquote":
            self._blockquote_depth = max(0, self._blockquote_depth - 1)

        elif tag == "p" and self._in_standalone_p:
            self._flush_paragraph()
            self._in_standalone_p = False
            self._reset_block()

        elif tag in ("strong", "b"):
            self._bold_depth = max(0, self._bold_depth - 1)

        elif tag in ("em", "i"):
            self._italic_depth = max(0, self._italic_depth - 1)

    def handle_data(self, data: str) -> None:
        if not (self._in_heading or self._in_standalone_p or self._in_li):
            return

        self._text_parts.append(data)

        # Record whether bold/italic was active *while text was collected*
        if self._bold_depth > 0:
            self._block_has_bold = True
        if self._italic_depth > 0:
            self._block_has_italic = True

    # ------------------------------------------------------------------
    # Result accessor
    # ------------------------------------------------------------------

    def get_sections(self) -> dict[str, list[Paragraph]]:
        return self._sections


def parse_notes_html(html: str) -> dict[str, list[Paragraph]]:
    """Parse TipTap HTML into per-section ``Paragraph`` blocks.

    Args:
        html: Raw HTML string produced by the TipTap editor.

    Returns:
        Dict with keys ``"background"``, ``"evidence"``, ``"facts"``, each
        mapping to a (possibly empty) list of ``Paragraph`` blocks ready for
        insertion into a ``HearingDocument``.
    """
    if not html or not html.strip():
        return {k: [] for k in _SECTION_KEYS}

    parser = _TipTapHTMLParser()
    parser.feed(html)
    return parser.get_sections()


def parse_notes_into_sections(html: str, sections: list[tuple[str, str]]) -> dict[str, list[Paragraph]]:
    """Split the notes among an uploaded template's dictated sections, given as (key,
    name) in order: each section's part follows a heading with its name (or, for the
    IAC's names, an alias such as "Findings of fact"). Text before any such heading is
    the first section's; other headings stay, as bold paragraphs."""
    if not sections:
        return {}
    aliases = {name.strip().lower(): key for key, name in reversed(sections)}
    # A section named as one of the IAC's (Facts, Findings of fact...) answers to all its names.
    by_canonical = {_SECTION_ALIAS_MAP[name]: key for name, key in aliases.items() if name in _SECTION_ALIAS_MAP}
    for alias, canonical in _SECTION_ALIAS_MAP.items():
        if canonical in by_canonical and alias not in aliases:
            aliases[alias] = by_canonical[canonical]
    first = sections[0][0]
    if not html or not html.strip():
        return {key: [] for key, _ in sections}
    parser = _TipTapHTMLParser(aliases=aliases, default_section=first)
    parser.feed(html)
    found = parser.get_sections()
    return {key: found.get(key, []) for key, _ in sections}
