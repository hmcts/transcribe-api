"""Parser for TipTap HTML editor content into structured document blocks.

Converts the HTML produced by the TipTap notes editor into ``Paragraph``
blocks grouped by the three recognised section headings (Background,
Evidence, Facts).
"""

from __future__ import annotations

from html.parser import HTMLParser

from transcribe_api.documents.lib.docx_style_profile import INDENT_INCREMENT_CM, STYLE_NUM_NORMAL, STYLE_QUOTATION, cm_to_pt
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

    def __init__(self) -> None:
        super().__init__()

        # Sections accumulate Paragraph blocks
        self._sections: dict[str, list[Paragraph]] = {k: [] for k in _SECTION_KEYS}
        self._current_section: str | None = None

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

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: ARG002
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
            heading_text = "".join(self._text_parts).strip().lower()
            self._current_section = _SECTION_ALIAS_MAP.get(heading_text)
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
