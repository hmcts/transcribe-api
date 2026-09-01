"""Centralized document style and layout profile for generated DOCX files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_LINE_SPACING
from docx.shared import Cm, Pt

from transcribe_api.runtime.logger import logger

if TYPE_CHECKING:
    from docx.document import Document as DocumentObject

DEFAULT_FONT_FAMILY = "Arial"
DEFAULT_FONT_SIZE_PT = 12
DEFAULT_SPACE_BEFORE_PT = 0
DEFAULT_SPACE_AFTER_PT = 0
DEFAULT_LINE_SPACING = 1.0
DEFAULT_MARGIN_CM = 2.0
INDENT_INCREMENT_CM = 1.0

STYLE_NORMAL = "Normal"
STYLE_HEADING_1 = "Heading 1"
STYLE_HEADING_2 = "Heading 2"
STYLE_HEADING_3 = "Heading 3"
STYLE_NUM_NORMAL = "NumNormal"
STYLE_QUOTATION = "Quotation"

CORE_PARAGRAPH_STYLES: tuple[str, ...] = (
    STYLE_NORMAL,
    STYLE_HEADING_1,
    STYLE_HEADING_2,
    STYLE_HEADING_3,
    STYLE_NUM_NORMAL,
    STYLE_QUOTATION,
)


def cm_to_pt(value_cm: float) -> float:
    return Cm(value_cm).pt


def indent_level_to_pt(level: int) -> float:
    return cm_to_pt(INDENT_INCREMENT_CM * max(level, 0))


def apply_global_document_formatting(document: DocumentObject) -> None:
    """Apply global layout defaults and ensure required paragraph styles exist."""
    _set_section_margins(document)
    ensure_core_styles(document)
    _prune_non_core_paragraph_styles(document)
    _enforce_six_style_visibility(document)
    _hide_non_core_latent_styles(document)


def resolve_style_name(document: DocumentObject, style_name: str | None, fallback: str = STYLE_NORMAL) -> str:
    """Return a safe style name that exists in the document."""
    if style_name and _style_exists(document, style_name):
        return style_name

    if style_name:
        logger.warning("DOCX style '%s' not found. Falling back to '%s'.", style_name, fallback)

    if _style_exists(document, fallback):
        return fallback

    logger.warning("Fallback DOCX style '%s' not found. Falling back to '%s'.", fallback, STYLE_NORMAL)
    return STYLE_NORMAL


def ensure_core_styles(document: DocumentObject) -> None:
    """Ensure project styles exist and carry required formatting rules."""
    _configure_normal_style(document)
    _configure_heading_styles(document)
    _configure_num_normal_style(document)
    _configure_quotation_style(document)


def _style_exists(document: DocumentObject, style_name: str) -> bool:
    try:
        document.styles[style_name]
    except KeyError:
        return False
    else:
        return True


def _set_section_margins(document: DocumentObject) -> None:
    margin = Cm(DEFAULT_MARGIN_CM)
    for section in document.sections:
        section.top_margin = margin
        section.right_margin = margin
        section.bottom_margin = margin
        section.left_margin = margin


def _apply_base_paragraph_defaults(style) -> None:
    style.font.name = DEFAULT_FONT_FAMILY
    style.font.size = Pt(DEFAULT_FONT_SIZE_PT)
    style.font.color.rgb = None
    style.font.highlight_color = None

    paragraph_format = style.paragraph_format
    paragraph_format.space_before = Pt(DEFAULT_SPACE_BEFORE_PT)
    paragraph_format.space_after = Pt(DEFAULT_SPACE_AFTER_PT)
    paragraph_format.line_spacing = DEFAULT_LINE_SPACING
    paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE


def _configure_normal_style(document: DocumentObject) -> None:
    normal = document.styles[STYLE_NORMAL]
    _apply_base_paragraph_defaults(normal)


def _configure_heading_styles(document: DocumentObject) -> None:
    heading_1 = document.styles[STYLE_HEADING_1]
    _apply_base_paragraph_defaults(heading_1)
    heading_1.font.bold = True
    heading_1.font.underline = False
    heading_1.font.italic = False

    heading_2 = document.styles[STYLE_HEADING_2]
    _apply_base_paragraph_defaults(heading_2)
    heading_2.font.bold = False
    heading_2.font.underline = True
    heading_2.font.italic = False

    heading_3 = document.styles[STYLE_HEADING_3]
    _apply_base_paragraph_defaults(heading_3)
    heading_3.font.bold = False
    heading_3.font.underline = False
    heading_3.font.italic = True


def _configure_num_normal_style(document: DocumentObject) -> None:
    if _style_exists(document, STYLE_NUM_NORMAL):
        num_normal = document.styles[STYLE_NUM_NORMAL]
    else:
        num_normal = document.styles.add_style(STYLE_NUM_NORMAL, WD_STYLE_TYPE.PARAGRAPH)
    num_normal.base_style = document.styles[STYLE_NORMAL]
    _apply_base_paragraph_defaults(num_normal)


def _configure_quotation_style(document: DocumentObject) -> None:
    if _style_exists(document, STYLE_QUOTATION):
        quotation = document.styles[STYLE_QUOTATION]
    else:
        quotation = document.styles.add_style(STYLE_QUOTATION, WD_STYLE_TYPE.PARAGRAPH)
    quotation.base_style = document.styles[STYLE_NORMAL]
    _apply_base_paragraph_defaults(quotation)
    quotation.font.italic = True
    quotation.paragraph_format.left_indent = Cm(2)


def _prune_non_core_paragraph_styles(document: DocumentObject) -> None:
    """Delete paragraph style definitions outside the required six."""
    for style in list(document.styles):
        if style.type != WD_STYLE_TYPE.PARAGRAPH:
            continue
        if style.name in CORE_PARAGRAPH_STYLES:
            continue
        try:
            style.delete()
        except Exception:
            logger.warning("Could not delete DOCX paragraph style '%s'.", style.name)


def _enforce_six_style_visibility(document: DocumentObject) -> None:
    """Make only the six project styles visible in the Word style gallery."""
    visible_priority = {
        STYLE_NORMAL: 1,
        STYLE_HEADING_1: 2,
        STYLE_HEADING_2: 3,
        STYLE_HEADING_3: 4,
        STYLE_NUM_NORMAL: 5,
        STYLE_QUOTATION: 6,
    }

    for style in document.styles:
        if style.type == WD_STYLE_TYPE.PARAGRAPH and style.name in CORE_PARAGRAPH_STYLES:
            style.hidden = False
            style.quick_style = True
            style.unhide_when_used = True
            style.priority = visible_priority[style.name]
            continue

        # Non-core styles are removed from UI (recommended/gallery lists).
        style.hidden = True
        style.quick_style = False
        style.unhide_when_used = False
        style.priority = 99


def _hide_non_core_latent_styles(document: DocumentObject) -> None:
    """Hide latent built-in styles so only the six project styles remain visible."""
    latent_styles = document.styles.latent_styles
    latent_styles.default_to_hidden = True
    latent_styles.default_to_locked = False
    latent_styles.default_to_quick_style = False

    for latent_style in latent_styles:
        if latent_style.name in CORE_PARAGRAPH_STYLES:
            latent_style.hidden = False
            latent_style.quick_style = True
            latent_style.unhide_when_used = True
            latent_style.priority = 1
            continue
        latent_style.hidden = True
        latent_style.quick_style = False
        latent_style.unhide_when_used = False
        latent_style.priority = 99
