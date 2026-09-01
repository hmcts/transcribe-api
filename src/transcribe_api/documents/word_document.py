"""Word document manipulation utilities.

This module provides a clean interface for working with Word documents,
including content control manipulation and direct text editing.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from transcribe_api.documents.models import ContentControl, ContentControlType, PopulationResult
from transcribe_api.documents.lib.docx_style_profile import STYLE_NORMAL, resolve_style_name

if TYPE_CHECKING:
    from docx.document import Document as DocxDocument
    from lxml.etree import _Element


class WordDocument:
    """A wrapper around python-docx Document with enhanced functionality.

    Provides methods for:
    - Reading and writing content controls
    - Finding and replacing text anywhere in the document
    - Appending paragraphs and content
    - Inspecting document structure
    """

    # Regex matching characters that are invalid in XML 1.0.
    # OOXML (.docx) uses XML 1.0 which only permits:
    #   #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
    _INVALID_XML_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]")

    def __init__(self, path: Path | str | None = None, document: DocxDocument | None = None) -> None:
        """Initialize with either a path to load or an existing document.

        Args:
            path: Path to a Word document to load
            document: An existing python-docx Document instance
        """
        if document is not None:
            self._document = document
        elif path is not None:
            self._document = Document(str(path))
        else:
            self._document = Document()

        self._path = Path(path) if path else None

    @classmethod
    def from_template(cls, template_path: Path | str) -> WordDocument:
        """Load a document from a template file.

        Args:
            template_path: Path to the Word template

        Returns:
            A new WordDocument instance

        Raises:
            FileNotFoundError: If template doesn't exist
        """
        path = Path(template_path)
        if not path.exists():
            msg = f"Template not found: {path}"
            raise FileNotFoundError(msg)
        return cls(path=path)

    @property
    def document(self) -> DocxDocument:
        """Access the underlying python-docx Document."""
        return self._document

    # ========================================================================
    # XML Safety Helpers
    # ========================================================================

    @classmethod
    def _sanitize_xml_text(cls, text: str) -> str:
        """Remove characters that are invalid in XML 1.0.

        OOXML (.docx) is XML 1.0 and will be flagged as corrupt by Word
        if any forbidden code-points slip into a ``w:t`` element.
        """
        return cls._INVALID_XML_CHARS_RE.sub("", text)

    def _iter_header_footer_elements(self):
        """Yield XML elements for header/footer parts that actually exist.

        Reads ``w:headerReference`` / ``w:footerReference`` elements
        directly from each section's ``w:sectPr``, then resolves the
        relationship to the target part.  This avoids going through
        python-docx's ``Header`` / ``Footer`` facades which can
        **create** new, empty header/footer parts as a side-effect —
        corrupting the document with phantom parts that Word flags as
        "unreadable content".
        """
        seen: set[str] = set()
        doc_part = self._document.part

        for section in self._document.sections:
            sect_pr = section._sectPr  # noqa: SLF001
            for ref in sect_pr.xpath("./*[local-name()='headerReference' or local-name()='footerReference']"):
                r_id = ref.get(qn("r:id"))
                if not r_id or r_id in seen:
                    continue
                seen.add(r_id)
                try:
                    element = doc_part.rels[r_id].target_part.element
                    if element is not None:
                        yield element
                except (KeyError, AttributeError):
                    continue

    @staticmethod
    def _extract_paragraph_text(para_elem: _Element) -> str:
        """Extract visible text from a raw ``w:p`` element.

        Mirrors python-docx ``Paragraph.text`` behaviour: ``w:t`` → text,
        ``w:br`` / ``w:cr`` → newline, ``w:tab`` → tab.
        """
        parts: list[str] = []
        for child in para_elem.iter():
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local == "t":
                parts.append(child.text or "")
            elif local in ("br", "cr"):
                parts.append("\n")
            elif local == "tab":
                parts.append("\t")
        return "".join(parts)

    # ========================================================================
    # Content Control Operations
    # ========================================================================

    def get_content_controls(self) -> list[ContentControl]:
        """Get all content controls in the document.

        Searches the main document body, headers, and footers.

        Returns:
            List of ContentControl objects representing each control
        """
        controls = []

        # Search main document body
        for sdt in self._document.element.xpath(".//*[local-name()='sdt']"):
            control = self._extract_control_info(sdt)
            if control:
                controls.append(control)

        # Search headers and footers (safely — avoids creating phantom parts)
        for hf_element in self._iter_header_footer_elements():
            for sdt in hf_element.xpath(".//*[local-name()='sdt']"):
                control = self._extract_control_info(sdt)
                if control:
                    controls.append(control)

        return controls

    def get_content_control_keys(self) -> set[str]:
        """Get just the keys (tag/alias values) of all content controls.

        Returns:
            Set of content control key strings
        """
        return {c.key for c in self.get_content_controls()}

    def populate_content_controls(  # noqa: C901
        self,
        data: dict[str, str],
        *,
        default_for_missing: str | None = None,
        style_missing_as_placeholder: bool = False,
        delete_if_empty: set[str] | None = None,
        skip_if_empty: set[str] | None = None,
        delete_marker: str = "[DELETE]",
    ) -> PopulationResult:
        """Populate content controls with data.

        Searches and populates controls in the main document body, headers, and footers.

        Args:
            data: Dictionary mapping control keys to values
            default_for_missing: If provided, use this value for controls not in data
            style_missing_as_placeholder: If True and default_for_missing is set,
                                          style the default value as bold red
            delete_if_empty: Set of control keys to delete entirely if no data provided
            skip_if_empty: Set of control keys to leave unchanged if no data provided
            delete_marker: Value that indicates a control should be deleted (default "[DELETE]")

        Returns:
            PopulationResult with details about what was populated
        """
        result = PopulationResult()
        delete_if_empty = delete_if_empty or set()
        skip_if_empty = skip_if_empty or set()
        controls_to_delete = []

        def process_sdt(sdt: _Element) -> None:
            """Process a single structured document tag."""
            key = self._get_control_key(sdt)
            if not key:
                return

            result.template_controls.add(key)

            if key in data:
                value = str(data[key])
                # Check if the value is the delete marker
                if value == delete_marker:
                    controls_to_delete.append(sdt)
                    result.populated_fields.add(key)
                else:
                    self._set_control_text(sdt, value)
                    result.populated_fields.add(key)
            elif key in skip_if_empty:
                # Leave unchanged - don't modify, don't count as missing
                result.populated_fields.add(key)
            elif key in delete_if_empty:
                # Mark for deletion (can't delete while iterating)
                controls_to_delete.append(sdt)
                result.populated_fields.add(key)  # Count as handled
            elif default_for_missing is not None:
                if style_missing_as_placeholder:
                    self._set_control_text_styled(
                        sdt,
                        default_for_missing,
                        bold=True,
                        color=RGBColor(255, 0, 0),  # Red
                    )
                else:
                    self._set_control_text(sdt, default_for_missing)
                result.populated_fields.add(key)
            else:
                result.missing_fields.add(key)

        # Process main document body
        for sdt in self._document.element.xpath(".//*[local-name()='sdt']"):
            process_sdt(sdt)

        # Process headers and footers (safely — avoids creating phantom parts)
        for hf_element in self._iter_header_footer_elements():
            for sdt in hf_element.xpath(".//*[local-name()='sdt']"):
                process_sdt(sdt)

        # Delete controls marked for removal
        for sdt in controls_to_delete:
            self._delete_content_control(sdt)

        result.unused_data_keys = set(data.keys()) - result.populated_fields
        return result

    def _delete_content_control(self, sdt: _Element) -> None:
        """Delete a content control element from the document.

        Args:
            sdt: The structured document tag element to delete
        """
        parent = sdt.getparent()
        if parent is None:
            return

        paragraph_parent = parent if parent.tag == qn("w:p") else None
        parent.remove(sdt)

        if paragraph_parent is not None and self._is_paragraph_empty(paragraph_parent):
            grandparent = paragraph_parent.getparent()
            if grandparent is not None:
                grandparent.remove(paragraph_parent)

    @staticmethod
    def _is_paragraph_empty(para: _Element) -> bool:
        """Return True when a paragraph contains no visible content."""
        has_non_whitespace_text = bool(para.xpath(".//*[local-name()='t' and normalize-space(text())!='']"))
        has_nested_controls = bool(para.xpath(".//*[local-name()='sdt']"))
        has_media = bool(para.xpath(".//*[local-name()='drawing' or local-name()='pict']"))
        return not (has_non_whitespace_text or has_nested_controls or has_media)

    def _extract_control_info(self, sdt: _Element) -> ContentControl | None:
        """Extract ContentControl info from a structured document tag."""
        # Try tag first
        tag = sdt.xpath("./*[local-name()='sdtPr']/*[local-name()='tag']")
        if tag:
            value = tag[0].get(qn("w:val"))
            if value:
                return ContentControl(key=value, control_type=ContentControlType.TAG)

        # Fall back to alias
        alias = sdt.xpath("./*[local-name()='sdtPr']/*[local-name()='alias']")
        if alias:
            value = alias[0].get(qn("w:val"))
            if value:
                return ContentControl(key=value, control_type=ContentControlType.ALIAS)

        return None

    def _get_control_key(self, sdt: _Element) -> str | None:
        """Get just the key from a structured document tag."""
        control = self._extract_control_info(sdt)
        return control.key if control else None

    def _set_control_text(self, sdt: _Element, value: str) -> None:
        """Set text in a content control, preserving the first run's formatting.

        Paragraph blocks (``\\n\\n``) are rendered as separate Word paragraphs.
        Single newlines (``\\n``) are rendered as ``w:br`` within a paragraph.

        When the value is multi-line, every paragraph inside the content
        control is forced to left-alignment so that short lines terminated
        by ``w:br`` are not fully justified with ugly word spacing.

        Trailing whitespace / newlines are stripped so the final visible
        line is the true last line of its paragraph.  Template highlighting
        is also removed.

        Handles both **block-level** content controls (whose ``sdtContent``
        contains ``w:p`` elements) and **inline/run-level** controls (whose
        ``sdtContent`` contains ``w:r`` elements directly).

        The control's ``sdtPr`` is cleaned to remove the plain-text
        restriction (``w:text``), the temporary flag (``w:temporary``),
        and the placeholder reference (``w:placeholder``) so Word does
        not try to auto-process the control and flag the file.
        """
        value = value.rstrip("\n\r ")

        # Clean sdtPr: remove w:text, w:temporary, w:placeholder so Word
        # won't flag the populated control as invalid or try to auto-remove it.
        self._clean_sdt_properties(sdt)

        sdt_content = sdt.find(qn("w:sdtContent"))
        if sdt_content is None:
            return

        paragraphs = sdt_content.xpath("./*[local-name()='p']")
        if paragraphs:
            # Block-level control — populate via paragraph replacement
            anchor_para = paragraphs[0]
            written_paragraphs = self._replace_with_paragraph_blocks(anchor_para, value)

            # Remove stale template paragraphs that were not overwritten.
            # Without this, old placeholder text leaks into the populated control
            # and can confuse Word's schema validation.
            written_ids = {id(p) for p in written_paragraphs}
            for old_para in list(sdt_content):
                if old_para.tag == qn("w:p") and id(old_para) not in written_ids:
                    sdt_content.remove(old_para)

            # Left-align only when we inserted soft line breaks inside paragraphs.
            if self._has_soft_line_breaks(value):
                self._force_left_alignment(sdt)

            self._strip_list_numbering(sdt)
        else:
            # Inline/run-level control — populate via run text replacement.
            # The sdtContent contains w:r elements directly (no w:p wrapper).
            runs = sdt_content.xpath("./*[local-name()='r']")
            if not runs:
                return

            # Keep the first run (preserving its rPr formatting), clear the rest
            first_run = runs[0]
            for run in runs[1:]:
                sdt_content.remove(run)

            # Also remove any proofErr or other non-run siblings
            for child in list(sdt_content):
                if child is not first_run and child.tag != qn("w:rPr"):
                    sdt_content.remove(child)

            # Clear existing text/br content in the kept run
            self._clear_run_content([first_run])

            # Write new text into the run
            self._populate_run_with_text(first_run, value)

        self._remove_control_highlighting(sdt)

    @staticmethod
    def _clean_sdt_properties(sdt: _Element) -> None:
        """Remove control-level properties that conflict with populated content.

        After we programmatically set the text of a content control the
        following ``sdtPr`` children are no longer appropriate and may
        cause Word to flag the document as containing "unreadable content":

        * ``w:text`` — restricts the control to plain text / single
          paragraph.  Our code may write multiple paragraphs or formatted
          runs, so we always remove it.
        * ``w:temporary`` — tells Word to auto-remove the control wrapper
          after editing.  Since we are editing programmatically, leaving
          this flag can confuse Word's post-open processing.
        * ``w:placeholder`` — references a glossary building-block for
          placeholder text.  Once real content is set the placeholder
          reference is stale and can trigger validation warnings.
        * ``w:showingPlcHdr`` — indicates the control is currently
          displaying its placeholder text.  Must be cleared when we
          write real content.
        """
        sdt_pr = sdt.find(qn("w:sdtPr"))
        if sdt_pr is None:
            return
        for tag_name in ("w:text", "w:temporary", "w:placeholder", "w:showingPlcHdr"):
            elem = sdt_pr.find(qn(tag_name))
            if elem is not None:
                sdt_pr.remove(elem)

    # ------------------------------------------------------------------ #
    #  _set_control_text helpers                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _clear_run_content(runs: list[_Element]) -> None:
        """Remove all ``w:t`` and ``w:br`` children from *runs*."""
        for run in runs:
            for child in run.xpath(".//*[local-name()='t'] | .//*[local-name()='br']"):
                child.getparent().remove(child)

    @staticmethod
    def _split_into_paragraphs(value: str) -> list[str]:
        """Split text into paragraph blocks on blank lines."""
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.split("\n\n") if normalized else [""]

    @classmethod
    def _has_soft_line_breaks(cls, value: str) -> bool:
        """True when any paragraph block contains internal single newlines."""
        return any("\n" in block for block in cls._split_into_paragraphs(value))

    @staticmethod
    def _replace_with_paragraph_blocks(anchor_para: _Element, value: str) -> list[_Element]:
        """Replace an anchor paragraph with one or more paragraph blocks.

        Returns:
            The paragraph elements written (first is ``anchor_para``).
        """
        blocks = WordDocument._split_into_paragraphs(value)
        if not blocks:
            blocks = [""]

        template_ppr = anchor_para.find(qn("w:pPr"))
        template_rpr = anchor_para.xpath("./*[local-name()='r']/*[local-name()='rPr']")
        template_rpr_elem = template_rpr[0] if template_rpr else None

        def write_block(para: _Element, block_text: str) -> None:
            for child in list(para):
                if child.tag == qn("w:r"):
                    para.remove(child)
            run = OxmlElement("w:r")
            if template_rpr_elem is not None:
                run.append(deepcopy(template_rpr_elem))
            WordDocument._populate_run_with_text(run, block_text)
            para.append(run)

        written_paragraphs = [anchor_para]
        write_block(anchor_para, blocks[0])

        current_para = anchor_para
        for block in blocks[1:]:
            new_para = OxmlElement("w:p")
            if template_ppr is not None:
                new_ppr = deepcopy(template_ppr)
                # Strip section-break properties so we don't duplicate
                # section boundaries — a common cause of "unreadable content".
                sect_pr = new_ppr.find(qn("w:sectPr"))
                if sect_pr is not None:
                    new_ppr.remove(sect_pr)
                new_para.append(new_ppr)
            current_para.addnext(new_para)
            write_block(new_para, block)
            written_paragraphs.append(new_para)
            current_para = new_para

        return written_paragraphs

    @staticmethod
    def _populate_run_with_text(run: _Element, value: str) -> None:
        """Append ``w:t`` and ``w:br`` elements to *run* for *value*.

        ``\\n`` becomes ``w:br``. Paragraph blocks are handled by callers.
        Characters that are invalid in XML 1.0 are stripped to prevent
        Word from flagging the document as containing unreadable content.
        """
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        normalized = WordDocument._sanitize_xml_text(normalized)
        lines = normalized.split("\n")

        for i, line in enumerate(lines):
            if line:
                t = OxmlElement("w:t")
                t.text = line
                t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                run.append(t)

            if i < len(lines) - 1:
                run.append(OxmlElement("w:br"))

    @classmethod
    def _force_left_alignment(cls, sdt: _Element) -> None:
        """Override paragraph justification to left-aligned inside *sdt*.

        Word fully justifies every line terminated by a ``w:br`` when the
        paragraph alignment is ``both`` (justify).  This produces large,
        ugly word spacing on short lines.  Forcing ``left`` avoids this.

        The justification may be set explicitly on the paragraph or
        inherited from the paragraph style, so we always ensure an
        explicit ``w:jc val="left"`` is present.
        """
        for para in sdt.xpath(".//*[local-name()='sdtContent']//*[local-name()='p']"):
            cls._force_paragraph_left_alignment(para)

    @staticmethod
    def _strip_list_numbering(sdt: _Element) -> None:
        """Remove Word list numbering (``w:numPr``) from paragraphs in *sdt*.

        When multi-line text provides its own structure (e.g. ``(a) …\\n(b) …``),
        the template's automatic list numbering would double up the markers.
        Stripping ``w:numPr`` lets the text-based enumeration take over.
        """
        for para in sdt.xpath(".//*[local-name()='sdtContent']//*[local-name()='p']"):
            ppr = para.find(qn("w:pPr"))
            if ppr is None:
                continue
            num_pr = ppr.find(qn("w:numPr"))
            if num_pr is not None:
                ppr.remove(num_pr)

    def _remove_control_highlighting(self, sdt: _Element) -> None:
        """Strip all highlighting / shading from a content control.

        Cleans both the ``sdtContent`` children (runs and paragraphs) and
        the control-level default run properties in ``sdtPr/rPr``.
        """
        sdt_content = sdt.find(qn("w:sdtContent"))
        if sdt_content is not None:
            self._remove_all_highlighting_from_element(sdt_content)

        sdt_pr = sdt.find(qn("w:sdtPr"))
        if sdt_pr is None:
            return
        rpr = sdt_pr.find(qn("w:rPr"))
        if rpr is None:
            return
        for tag in ("w:highlight", "w:shd"):
            elem = rpr.find(qn(tag))
            if elem is not None:
                rpr.remove(elem)

    def _set_control_text_styled(  # noqa: C901
        self,
        sdt: _Element,
        value: str,
        *,
        bold: bool = False,
        color: RGBColor | None = None,
    ) -> None:
        """Set text in a content control with custom styling.

        Args:
            sdt: The structured document tag element
            value: Text value to set
            bold: Whether to make text bold
            color: RGB color for the text
        """
        # Clean control properties (same as _set_control_text)
        self._clean_sdt_properties(sdt)

        # Find or create the run element
        runs = sdt.xpath(".//*[local-name()='sdtContent']//*[local-name()='r']")
        if not runs:
            return

        run = runs[0]

        # Sanitize value before writing to XML
        value = self._sanitize_xml_text(value)

        # Set the text
        text_nodes = run.xpath(".//*[local-name()='t']")
        if text_nodes:
            text_nodes[0].text = value
            # Clear any additional text nodes
            for node in text_nodes[1:]:
                node.text = ""
        else:
            # Create a text element
            t = OxmlElement("w:t")
            t.text = value
            run.append(t)

        # Get or create run properties
        run_props = run.find(qn("w:rPr"))
        if run_props is None:
            run_props = OxmlElement("w:rPr")
            run.insert(0, run_props)

        # Set bold
        if bold:
            b = run_props.find(qn("w:b"))
            if b is None:
                b = OxmlElement("w:b")
                run_props.append(b)

        # Set color
        if color:
            color_elem = run_props.find(qn("w:color"))
            if color_elem is None:
                color_elem = OxmlElement("w:color")
                run_props.append(color_elem)
            # Convert RGB to hex string
            hex_color = f"{color[0]:02X}{color[1]:02X}{color[2]:02X}"
            color_elem.set(qn("w:val"), hex_color)

        # Clear text from other runs
        for other_run in runs[1:]:
            for t in other_run.xpath(".//*[local-name()='t']"):
                t.text = ""

    def _remove_highlighting(self, run: _Element) -> None:
        """Remove highlighting and shading from a run element.

        This ensures injected content does not retain any placeholder highlighting
        from the template.

        Args:
            run: The run (w:r) element to process
        """
        run_props = run.find(qn("w:rPr"))
        if run_props is None:
            return

        # Remove text highlighting (w:highlight)
        highlight = run_props.find(qn("w:highlight"))
        if highlight is not None:
            run_props.remove(highlight)

        # Remove shading/background color (w:shd)
        shading = run_props.find(qn("w:shd"))
        if shading is not None:
            run_props.remove(shading)

        # Remove background color (w:background) - less common but possible
        background = run_props.find(qn("w:background"))
        if background is not None:
            run_props.remove(background)

    def _remove_paragraph_highlighting(self, paragraph_element: _Element) -> None:
        """Remove highlighting and shading from a paragraph element.

        Args:
            paragraph_element: The paragraph (w:p) element to process
        """
        para_props = paragraph_element.find(qn("w:pPr"))
        if para_props is None:
            return

        # Remove paragraph shading
        shading = para_props.find(qn("w:shd"))
        if shading is not None:
            para_props.remove(shading)

    def _remove_all_highlighting_from_element(self, element: _Element) -> None:
        """Remove all highlighting from an element and its children.

        Removes highlighting from:
        - All runs (w:r) within the element
        - All paragraphs (w:p) within the element

        Args:
            element: The parent element to process
        """
        # Remove from all runs
        for run in element.xpath(".//*[local-name()='r']"):
            self._remove_highlighting(run)

        # Remove from all paragraphs
        for para in element.xpath(".//*[local-name()='p']"):
            self._remove_paragraph_highlighting(para)

    # ========================================================================
    # Text Search and Replace Operations
    # ========================================================================

    def find_and_replace(self, pattern: str, replacement: str, *, regex: bool = False) -> int:
        """Find and replace text throughout the document.

        Works on paragraphs in the main body, headers, footers, and tables.

        Args:
            pattern: Text or regex pattern to find
            replacement: Text to replace with
            regex: If True, treat pattern as a regular expression

        Returns:
            Number of replacements made
        """
        count = 0

        # Replace in main body paragraphs
        for paragraph in self._document.paragraphs:
            count += self._replace_in_paragraph(paragraph, pattern, replacement, regex=regex)

        # Replace in tables
        for table in self._document.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        count += self._replace_in_paragraph(paragraph, pattern, replacement, regex=regex)

        # Replace in headers and footers (safely — avoids creating phantom parts)
        for hf_element in self._iter_header_footer_elements():
            for para_elem in hf_element.xpath("./*[local-name()='p']"):
                count += self._replace_in_paragraph(para_elem, pattern, replacement, regex=regex)
            # Also handle paragraphs inside tables within headers/footers
            for para_elem in hf_element.xpath(".//*[local-name()='tbl']//*[local-name()='p']"):
                count += self._replace_in_paragraph(para_elem, pattern, replacement, regex=regex)

        return count

    def _replace_in_paragraph(self, paragraph, pattern: str, replacement: str, *, regex: bool) -> int:
        """Replace text in a single paragraph.

        Also removes any highlighting from the paragraph and its runs.
        When the resulting text contains newlines, the paragraph is forced
        to left-alignment to prevent ugly justified word spacing on lines
        terminated by ``w:br``.

        Args:
            paragraph: A python-docx ``Paragraph`` object **or** a raw
                ``w:p`` lxml element (for safe header/footer processing).
        """
        # Support both python-docx Paragraph objects and raw w:p elements
        if hasattr(paragraph, "_element"):
            full_text = paragraph.text
            para_elem = paragraph._element  # noqa: SLF001
        else:
            para_elem = paragraph
            full_text = self._extract_paragraph_text(para_elem)

        if not full_text:
            return 0

        if regex:
            new_text, count = re.subn(pattern, replacement, full_text)
        else:
            count = full_text.count(pattern)
            new_text = full_text.replace(pattern, replacement)

        if count == 0:
            return 0

        written_paragraphs = self._replace_with_paragraph_blocks(para_elem, new_text)

        # Force left-alignment only when we inserted soft line breaks.
        if self._has_soft_line_breaks(new_text):
            for para in written_paragraphs:
                self._force_paragraph_left_alignment(para)

        for para in written_paragraphs:
            self._remove_paragraph_highlighting(para)
            self._remove_all_highlighting_from_element(para)

        return count

    @staticmethod
    def _force_paragraph_left_alignment(para: _Element) -> None:
        """Force a single ``w:p`` element to left-alignment.

        Prevents Word from fully justifying every ``w:br``-terminated line
        when the paragraph style uses ``both`` (justify) alignment.
        """
        ppr = para.find(qn("w:pPr"))
        if ppr is None:
            ppr = OxmlElement("w:pPr")
            para.insert(0, ppr)

        jc = ppr.find(qn("w:jc"))
        if jc is None:
            jc = OxmlElement("w:jc")
            ppr.append(jc)

        jc.set(qn("w:val"), "left")

    def find_text(self, pattern: str, *, regex: bool = False) -> list[str]:
        """Find all occurrences of text in the document.

        Args:
            pattern: Text or regex pattern to find
            regex: If True, treat pattern as a regular expression

        Returns:
            List of matching text strings
        """
        matches = []

        for paragraph in self._document.paragraphs:
            text = paragraph.text
            if regex:
                matches.extend(re.findall(pattern, text))
            elif pattern in text:
                matches.append(pattern)

        return matches

    # ========================================================================
    # Content Addition Operations
    # ========================================================================

    def append_paragraph(
        self,
        text: str,
        style: str | None = None,
        *,
        bold: bool = False,
        font_size: int | None = None,
    ) -> None:
        """Append a new paragraph to the document.

        Args:
            text: The text content
            style: Optional Word style name (e.g., 'Heading 1')
            bold: Whether to make text bold
            font_size: Font size in points
        """
        style_name = resolve_style_name(self._document, style, fallback=STYLE_NORMAL)
        paragraph = self._document.add_paragraph(style=style_name)
        run = paragraph.add_run(text)

        if bold:
            run.bold = True
        if font_size:
            run.font.size = Pt(font_size)

    def append_heading(self, text: str, level: int = 1) -> None:
        """Append a heading to the document.

        Args:
            text: Heading text
            level: Heading level (1-9)
        """
        self._document.add_heading(text, level=level)

    def append_table(self, rows: list[list[str]], *, header: bool = True) -> None:
        """Append a table to the document.

        Args:
            rows: List of rows, each row is a list of cell values
            header: If True, format first row as header
        """
        if not rows:
            return

        num_cols = len(rows[0])
        table = self._document.add_table(rows=len(rows), cols=num_cols)

        for i, row_data in enumerate(rows):
            row = table.rows[i]
            for j, cell_text in enumerate(row_data):
                row.cells[j].text = str(cell_text)

                # Bold header row
                if header and i == 0:
                    for paragraph in row.cells[j].paragraphs:
                        for run in paragraph.runs:
                            run.bold = True

    # ========================================================================
    # Image Operations
    # ========================================================================

    def replace_crest_image(self, image_path: Path | str) -> bool:
        """Replace the crest image (first image in the document body).

        The template places the judicial crest as the very first embedded
        image in the main body.  This method swaps the underlying binary
        data of that first image so the layout, sizing and positioning
        defined in the template are preserved.  All other images in the
        document are left untouched.

        Args:
            image_path: Path to the replacement image file.

        Returns:
            ``True`` if an image was replaced, ``False`` if no images
            were found in the document body.
        """
        image_path = Path(image_path)
        if not image_path.exists():
            msg = f"Image not found: {image_path}"
            raise FileNotFoundError(msg)

        image_data = image_path.read_bytes()

        part = self._document.part
        element = self._document.element

        # Find the first blip (embedded image reference) in document order
        for blip in element.xpath(".//*[local-name()='blip']"):
            embed = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
            if not embed or embed not in part.rels:
                continue

            target = part.rels[embed].target_part
            target._blob = image_data  # noqa: SLF001
            return True

        return False

    # ========================================================================
    # Save Operations
    # ========================================================================

    def save(self, path: Path | str) -> Path:
        """Save the document to a file.

        Args:
            path: Output file path

        Returns:
            The path where the document was saved
        """
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._document.save(str(output_path))
        return output_path
