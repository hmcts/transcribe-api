"""Authoritative style definitions extracted from vanilla-template.docx.

Every paragraph style, numbering definition, page layout, and visibility
rule that the vanilla Word template carries is reproduced here in code.
Calling ``apply_template_styles(document)`` on a blank python-docx
``Document`` produces a style profile identical to the template.

Source template: backend/data/vanilla-template.docx
Extraction script: scripts/extract_template_styles.py
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm

if TYPE_CHECKING:
    from docx.document import Document as DocumentObject

# =====================================================================
#  Style name constants
# =====================================================================

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

# =====================================================================
#  Layout constants (from vanilla-template.docx Section 0)
# =====================================================================

PAGE_WIDTH_CM = 21.0
PAGE_HEIGHT_CM = 29.7
MARGIN_TOP_CM = 2.54
MARGIN_BOTTOM_CM = 2.54
MARGIN_LEFT_CM = 2.0
MARGIN_RIGHT_CM = 2.0
HEADER_DISTANCE_CM = 1.25
FOOTER_DISTANCE_CM = 1.25

# =====================================================================
#  Typography constants (effective values after theme resolution)
# =====================================================================

# The template Normal style uses rFonts asciiTheme="minorBidi" /
# hAnsiTheme="minorBidi" which resolves to Arial through the Office
# theme's minor font Arab-script mapping.
FONT_THEME_REF = "minorBidi"
FONT_FAMILY_EFFECTIVE = "Arial"
FONT_FAMILY_CS = "Arial"
FONT_SIZE_PT = 12
FONT_SIZE_HALF_POINTS = 24
SPACE_BEFORE_TWIPS = 240
LIST_ITEM_SPACE_BEFORE_TWIPS = 120
LINE_SPACING_TWIPS = 240
INDENT_INCREMENT_TWIPS = 567
QUOTATION_BASE_INDENT_TWIPS = 1134  # 2 cm — base left indent for the Quotation style

# Convenience re-exports used by other modules
DEFAULT_FONT_FAMILY = FONT_FAMILY_EFFECTIVE
DEFAULT_FONT_SIZE_PT = FONT_SIZE_PT
DEFAULT_SPACE_BEFORE_PT = 12
DEFAULT_LINE_SPACING = 1.0
DEFAULT_MARGIN_CM = MARGIN_LEFT_CM
INDENT_INCREMENT_CM = 1.0
QUOTATION_BASE_INDENT_CM = 2.0

# =====================================================================
#  Numbering definition constants (abstractNum 0 from template)
# =====================================================================

_NUMBERING_LEVELS: tuple[dict, ...] = (
    {"ilvl": "0", "numFmt": "decimal", "lvlText": "%1.", "ind_left": "567", "ind_hang": "567", "tab": "567"},
    {"ilvl": "1", "numFmt": "lowerLetter", "lvlText": "(%2)", "ind_left": "1134", "ind_hang": "567", "tab": "1134"},
    {"ilvl": "2", "numFmt": "lowerRoman", "lvlText": "(%3)", "ind_left": "1701", "ind_hang": "567", "tab": "1701"},
    {"ilvl": "3", "numFmt": "decimal", "lvlText": "(%4)", "ind_left": "2268", "ind_hang": "567", "tab": "2268"},
    {"ilvl": "4", "numFmt": "lowerLetter", "lvlText": "(%5)", "ind_left": "2835", "ind_hang": "567", "tab": "2835"},
    {"ilvl": "5", "numFmt": "lowerRoman", "lvlText": "(%6)", "ind_left": "2160", "ind_hang": "360", "tab": "2160"},
    {"ilvl": "6", "numFmt": "decimal", "lvlText": "%7.", "ind_left": "2520", "ind_hang": "360", "tab": "2520"},
    {"ilvl": "7", "numFmt": "lowerLetter", "lvlText": "%8.", "ind_left": "2880", "ind_hang": "360", "tab": "2880"},
    {"ilvl": "8", "numFmt": "lowerRoman", "lvlText": "%9.", "ind_left": "3240", "ind_hang": "360", "tab": "3240"},
)


# =====================================================================
#  Unit helpers
# =====================================================================


def cm_to_pt(value_cm: float) -> float:
    """Convert centimetres to points."""
    return Cm(value_cm).pt


def indent_level_to_pt(level: int) -> float:
    """Convert an indent level (0, 1, 2 …) to points using the template's 1 cm increment."""
    return cm_to_pt(INDENT_INCREMENT_CM * max(level, 0))


# =====================================================================
#  Public entry point
# =====================================================================


def apply_template_styles(document: DocumentObject) -> None:
    """Configure *document* to match the vanilla-template.docx profile.

    This is the single call needed to prepare a blank ``Document()``
    with the correct page layout, all six paragraph styles, numbering
    definitions, and style-gallery visibility.
    """
    _apply_page_layout(document)
    _apply_doc_defaults(document)
    _configure_normal(document)
    _configure_heading_1(document)
    _configure_heading_2(document)
    _configure_heading_3(document)
    numbering_num_id = _ensure_numbering_definition(document)
    _configure_num_normal(document, numbering_num_id)
    _configure_quotation(document)
    _create_linked_character_styles(document)
    _prune_non_core_paragraph_styles(document)
    _enforce_style_visibility(document)
    _hide_non_core_latent_styles(document)


def resolve_style_name(
    document: DocumentObject,
    style_name: str | None,
    fallback: str = STYLE_NORMAL,
) -> str:
    """Return a safe style name that exists in *document*."""
    if style_name and _style_exists(document, style_name):
        return style_name
    if _style_exists(document, fallback):
        return fallback
    return STYLE_NORMAL


# =====================================================================
#  Page layout
# =====================================================================


def _apply_page_layout(document: DocumentObject) -> None:
    for section in document.sections:
        section.page_width = Cm(PAGE_WIDTH_CM)
        section.page_height = Cm(PAGE_HEIGHT_CM)
        section.top_margin = Cm(MARGIN_TOP_CM)
        section.bottom_margin = Cm(MARGIN_BOTTOM_CM)
        section.left_margin = Cm(MARGIN_LEFT_CM)
        section.right_margin = Cm(MARGIN_RIGHT_CM)
        section.header_distance = Cm(HEADER_DISTANCE_CM)
        section.footer_distance = Cm(FOOTER_DISTANCE_CM)


# =====================================================================
#  Document defaults (docDefaults)
# =====================================================================


def _apply_doc_defaults(document: DocumentObject) -> None:
    """Set docDefaults to match the template's rPrDefault and pPrDefault."""
    styles_elem = document.styles.element
    doc_defaults = styles_elem.find(qn("w:docDefaults"))
    if doc_defaults is None:
        doc_defaults = OxmlElement("w:docDefaults")
        styles_elem.insert(0, doc_defaults)

    # rPrDefault
    rpr_default = doc_defaults.find(qn("w:rPrDefault"))
    if rpr_default is None:
        rpr_default = OxmlElement("w:rPrDefault")
        doc_defaults.append(rpr_default)
    rpr = rpr_default.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        rpr_default.append(rpr)

    _set_or_replace_child(
        rpr,
        "w:rFonts",
        {
            qn("w:ascii"): FONT_FAMILY_EFFECTIVE,
            qn("w:eastAsia"): FONT_FAMILY_EFFECTIVE,
            qn("w:hAnsi"): FONT_FAMILY_EFFECTIVE,
            qn("w:cs"): FONT_FAMILY_CS,
        },
    )
    _set_or_replace_child(
        rpr,
        "w:lang",
        {
            qn("w:val"): "en-GB",
            qn("w:eastAsia"): "en-GB",
            qn("w:bidi"): "ar-SA",
        },
    )

    # pPrDefault — ensure global single line spacing is explicit
    ppr_default = doc_defaults.find(qn("w:pPrDefault"))
    if ppr_default is None:
        ppr_default = OxmlElement("w:pPrDefault")
        doc_defaults.append(ppr_default)
    ppr = _ensure_child(ppr_default, "w:pPr")
    spacing = _ensure_child(ppr, "w:spacing")
    spacing.set(qn("w:line"), str(LINE_SPACING_TWIPS))
    spacing.set(qn("w:lineRule"), "auto")


# =====================================================================
#  Normal style
# =====================================================================


def _configure_normal(document: DocumentObject) -> None:
    """Normal: Arial 12pt, no explicit paragraph formatting."""
    normal = document.styles[STYLE_NORMAL]
    elem = normal.element

    ppr = elem.find(qn("w:pPr"))
    if ppr is not None:
        elem.remove(ppr)

    rpr = OxmlElement("w:rPr")
    rpr.append(
        _make_elem(
            "w:rFonts",
            {
                qn("w:ascii"): FONT_FAMILY_EFFECTIVE,
                qn("w:hAnsi"): FONT_FAMILY_EFFECTIVE,
                qn("w:cs"): FONT_FAMILY_CS,
            },
        )
    )
    rpr.append(_make_elem("w:sz", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}))
    rpr.append(_make_elem("w:szCs", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}))
    _replace_child(elem, "w:rPr", rpr)


# =====================================================================
#  Heading styles
# =====================================================================


def _shared_heading_ppr(outline_level: str) -> OxmlElement:
    """Build the paragraph-properties element common to all three headings."""
    ppr = OxmlElement("w:pPr")
    ppr.append(OxmlElement("w:keepNext"))
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:before"), str(SPACE_BEFORE_TWIPS))
    spacing.set(qn("w:line"), str(LINE_SPACING_TWIPS))
    spacing.set(qn("w:lineRule"), "auto")
    ppr.append(spacing)
    jc = OxmlElement("w:jc")
    jc.set(qn("w:val"), "both")
    ppr.append(jc)
    outline = OxmlElement("w:outlineLvl")
    outline.set(qn("w:val"), outline_level)
    ppr.append(outline)
    return ppr


def _heading_rpr_base() -> OxmlElement:
    """Run-properties shared across headings: cs font = Arial."""
    rpr = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:cs"), FONT_FAMILY_CS)
    rpr.append(fonts)
    return rpr


def _configure_heading_1(document: DocumentObject) -> None:
    """Heading 1: bold, keepNext, justify, outlineLevel 0."""
    h1 = document.styles[STYLE_HEADING_1]
    h1.base_style = document.styles[STYLE_NORMAL]
    elem = h1.element

    # Set next-paragraph style to Normal
    _set_or_replace_child(elem, "w:next", {qn("w:val"): "Normal"})

    _replace_child(elem, "w:pPr", _shared_heading_ppr("0"))

    rpr = _heading_rpr_base()
    rpr.append(OxmlElement("w:b"))
    _replace_child(elem, "w:rPr", rpr)


def _configure_heading_2(document: DocumentObject) -> None:
    """Heading 2: underline single, keepNext, justify, outlineLevel 1."""
    h2 = document.styles[STYLE_HEADING_2]
    h2.base_style = document.styles[STYLE_NORMAL]
    elem = h2.element

    _set_or_replace_child(elem, "w:next", {qn("w:val"): "Normal"})

    ppr = _shared_heading_ppr("1")
    _replace_child(elem, "w:pPr", ppr)

    rpr = _heading_rpr_base()
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rpr.append(u)
    _replace_child(elem, "w:rPr", rpr)

    # Ensure unhideWhenUsed
    if elem.find(qn("w:unhideWhenUsed")) is None:
        elem.append(OxmlElement("w:unhideWhenUsed"))


def _configure_heading_3(document: DocumentObject) -> None:
    """Heading 3: italic + iCs, keepNext, justify, outlineLevel 2."""
    h3 = document.styles[STYLE_HEADING_3]
    h3.base_style = document.styles[STYLE_NORMAL]
    elem = h3.element

    _set_or_replace_child(elem, "w:next", {qn("w:val"): "Normal"})

    ppr = _shared_heading_ppr("2")
    _replace_child(elem, "w:pPr", ppr)

    rpr = _heading_rpr_base()
    rpr.append(OxmlElement("w:i"))
    rpr.append(OxmlElement("w:iCs"))
    _replace_child(elem, "w:rPr", rpr)

    if elem.find(qn("w:unhideWhenUsed")) is None:
        elem.append(OxmlElement("w:unhideWhenUsed"))


# =====================================================================
#  NumNormal (custom paragraph style + numbering definition)
# =====================================================================


def _configure_num_normal(document: DocumentObject, num_id: int) -> None:
    """NumNormal: based on Normal, justify, reduced space-before, linked numbering."""
    if _style_exists(document, STYLE_NUM_NORMAL):
        nn = document.styles[STYLE_NUM_NORMAL]
    else:
        nn = document.styles.add_style(STYLE_NUM_NORMAL, WD_STYLE_TYPE.PARAGRAPH)
    nn.base_style = document.styles[STYLE_NORMAL]
    elem = nn.element

    # Mark as custom
    elem.set(qn("w:customStyle"), "1")

    ppr = OxmlElement("w:pPr")
    numpr = OxmlElement("w:numPr")
    numid_elem = OxmlElement("w:numId")
    numid_elem.set(qn("w:val"), str(num_id))
    numpr.append(numid_elem)
    ppr.append(numpr)
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:before"), str(LIST_ITEM_SPACE_BEFORE_TWIPS))
    spacing.set(qn("w:line"), str(LINE_SPACING_TWIPS))
    spacing.set(qn("w:lineRule"), "auto")
    ppr.append(spacing)
    jc = OxmlElement("w:jc")
    jc.set(qn("w:val"), "both")
    ppr.append(jc)
    _replace_child(elem, "w:pPr", ppr)

    rpr = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:cs"), FONT_FAMILY_CS)
    rpr.append(fonts)
    _replace_child(elem, "w:rPr", rpr)


# =====================================================================
#  Quotation (custom paragraph style)
# =====================================================================


def _configure_quotation(document: DocumentObject) -> None:
    """Quotation: italic + iCs, left indent 1134 twips (2 cm), justify."""
    if _style_exists(document, STYLE_QUOTATION):
        q = document.styles[STYLE_QUOTATION]
    else:
        q = document.styles.add_style(STYLE_QUOTATION, WD_STYLE_TYPE.PARAGRAPH)
    q.base_style = document.styles[STYLE_NORMAL]
    elem = q.element

    elem.set(qn("w:customStyle"), "1")

    ppr = OxmlElement("w:pPr")
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:before"), str(SPACE_BEFORE_TWIPS))
    spacing.set(qn("w:line"), str(LINE_SPACING_TWIPS))
    spacing.set(qn("w:lineRule"), "auto")
    ppr.append(spacing)
    ind = OxmlElement("w:ind")
    ind.set(qn("w:left"), str(QUOTATION_BASE_INDENT_TWIPS))
    ppr.append(ind)
    jc = OxmlElement("w:jc")
    jc.set(qn("w:val"), "both")
    ppr.append(jc)
    _replace_child(elem, "w:pPr", ppr)

    rpr = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:cs"), FONT_FAMILY_CS)
    rpr.append(fonts)
    rpr.append(OxmlElement("w:i"))
    rpr.append(OxmlElement("w:iCs"))
    _replace_child(elem, "w:rPr", rpr)


# =====================================================================
#  Linked character styles
# =====================================================================


def _create_linked_character_styles(document: DocumentObject) -> None:
    """Create the five linked character styles that the template carries."""
    _char_style_defs: list[dict] = [
        {
            "style_id": "Heading1Char",
            "name": "Heading 1 Char",
            "link": "Heading1",
            "rpr_children": [
                _make_rfonts_char(),
                _make_elem("w:b"),
                _make_elem("w:sz", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}),
                _make_elem("w:szCs", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}),
            ],
        },
        {
            "style_id": "Heading2Char",
            "name": "Heading 2 Char",
            "link": "Heading2",
            "rpr_children": [
                _make_rfonts_char(),
                _make_elem("w:sz", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}),
                _make_elem("w:szCs", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}),
                _make_elem("w:u", {qn("w:val"): "single"}),
            ],
        },
        {
            "style_id": "Heading3Char",
            "name": "Heading 3 Char",
            "link": "Heading3",
            "rpr_children": [
                _make_rfonts_char(),
                _make_elem("w:i"),
                _make_elem("w:iCs"),
                _make_elem("w:sz", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}),
                _make_elem("w:szCs", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}),
            ],
        },
        {
            "style_id": "NumNormalChar",
            "name": "NumNormal Char",
            "link": "NumNormal",
            "rpr_children": [
                _make_rfonts_char(),
                _make_elem("w:sz", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}),
                _make_elem("w:szCs", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}),
            ],
        },
        {
            "style_id": "QuotationChar",
            "name": "Quotation Char",
            "link": "Quotation",
            "rpr_children": [
                _make_rfonts_char(),
                _make_elem("w:i"),
                _make_elem("w:iCs"),
                _make_elem("w:sz", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}),
                _make_elem("w:szCs", {qn("w:val"): str(FONT_SIZE_HALF_POINTS)}),
            ],
        },
    ]

    styles_elem = document.styles.element
    for defn in _char_style_defs:
        existing = styles_elem.findall(
            f".//w:style[@w:styleId='{defn['style_id']}']",
            {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"},
        )
        if existing:
            for e in existing:
                e.getparent().remove(e)

        style_elem = OxmlElement("w:style")
        style_elem.set(qn("w:type"), "character")
        style_elem.set(qn("w:customStyle"), "1")
        style_elem.set(qn("w:styleId"), defn["style_id"])

        name_elem = OxmlElement("w:name")
        name_elem.set(qn("w:val"), defn["name"])
        style_elem.append(name_elem)

        based_on = OxmlElement("w:basedOn")
        based_on.set(qn("w:val"), "DefaultParagraphFont")
        style_elem.append(based_on)

        link = OxmlElement("w:link")
        link.set(qn("w:val"), defn["link"])
        style_elem.append(link)

        rpr = OxmlElement("w:rPr")
        for child in defn["rpr_children"]:
            rpr.append(child)
        style_elem.append(rpr)

        styles_elem.append(style_elem)

    # Also set the link element on each paragraph style pointing back
    _link_map = {
        STYLE_HEADING_1: "Heading1Char",
        STYLE_HEADING_2: "Heading2Char",
        STYLE_HEADING_3: "Heading3Char",
        STYLE_NUM_NORMAL: "NumNormalChar",
        STYLE_QUOTATION: "QuotationChar",
    }
    for style_name, char_id in _link_map.items():
        if _style_exists(document, style_name):
            elem = document.styles[style_name].element
            _set_or_replace_child(elem, "w:link", {qn("w:val"): char_id})


# =====================================================================
#  Numbering definitions
# =====================================================================


def _build_numbering_level(lvl_def: dict) -> OxmlElement:
    """Build a single ``w:lvl`` element from a level definition dict."""
    lvl = OxmlElement("w:lvl")
    lvl.set(qn("w:ilvl"), lvl_def["ilvl"])

    lvl.append(_make_elem("w:start", {qn("w:val"): "1"}))
    lvl.append(_make_elem("w:numFmt", {qn("w:val"): lvl_def["numFmt"]}))

    if lvl_def["ilvl"] == "0":
        lvl.append(_make_elem("w:pStyle", {qn("w:val"): "NumNormal"}))

    lvl.append(_make_elem("w:lvlText", {qn("w:val"): lvl_def["lvlText"]}))
    lvl.append(_make_elem("w:lvlJc", {qn("w:val"): "left"}))

    ppr = OxmlElement("w:pPr")
    tabs = OxmlElement("w:tabs")
    tabs.append(_make_elem("w:tab", {qn("w:val"): "num", qn("w:pos"): lvl_def["tab"]}))
    ppr.append(tabs)
    ppr.append(_make_elem("w:ind", {qn("w:left"): lvl_def["ind_left"], qn("w:hanging"): lvl_def["ind_hang"]}))
    lvl.append(ppr)

    rpr = OxmlElement("w:rPr")
    rpr.append(_make_elem("w:rFonts", {qn("w:hint"): "default"}))
    lvl.append(rpr)

    return lvl


def _ensure_numbering_definition(document: DocumentObject) -> int:
    """Create the template's 9-level numbering scheme and return its numId.

    The NumNormal style links to this definition via ``w:numPr/w:numId``.
    Level 0 is bound to the NumNormal paragraph style (``w:pStyle``).
    """
    numbering_elem = document.part.numbering_part.element

    abstract_num = OxmlElement("w:abstractNum")
    abstract_num.set(qn("w:abstractNumId"), "0")
    abstract_num.append(_make_elem("w:nsid", {qn("w:val"): "14D60EB8"}))
    abstract_num.append(_make_elem("w:multiLevelType", {qn("w:val"): "hybridMultilevel"}))
    abstract_num.append(_make_elem("w:tmpl", {qn("w:val"): "2136A0AC"}))

    for lvl_def in _NUMBERING_LEVELS:
        abstract_num.append(_build_numbering_level(lvl_def))

    for old in list(numbering_elem.findall(qn("w:abstractNum"))):
        numbering_elem.remove(old)
    for old in list(numbering_elem.findall(qn("w:num"))):
        numbering_elem.remove(old)

    numbering_elem.append(abstract_num)

    num = OxmlElement("w:num")
    num.set(qn("w:numId"), "1")
    num.append(_make_elem("w:abstractNumId", {qn("w:val"): "0"}))
    numbering_elem.append(num)

    return 1


# =====================================================================
#  Style visibility and cleanup
# =====================================================================

_STYLE_PRIORITY = {
    STYLE_NORMAL: 1,
    STYLE_HEADING_1: 2,
    STYLE_HEADING_2: 3,
    STYLE_HEADING_3: 4,
    STYLE_NUM_NORMAL: 5,
    STYLE_QUOTATION: 6,
}


def _prune_non_core_paragraph_styles(document: DocumentObject) -> None:
    """Delete paragraph style definitions outside the required six."""
    import contextlib

    for style in list(document.styles):
        if style.type != WD_STYLE_TYPE.PARAGRAPH:
            continue
        if style.name in CORE_PARAGRAPH_STYLES:
            continue
        with contextlib.suppress(Exception):
            style.delete()


def _enforce_style_visibility(document: DocumentObject) -> None:
    """Make only the six core styles visible in Word's style gallery."""
    for style in document.styles:
        if style.type == WD_STYLE_TYPE.PARAGRAPH and style.name in CORE_PARAGRAPH_STYLES:
            style.hidden = False
            style.quick_style = True
            style.unhide_when_used = True
            style.priority = _STYLE_PRIORITY[style.name]
        else:
            style.hidden = True
            style.quick_style = False
            style.unhide_when_used = False
            style.priority = 99


def _hide_non_core_latent_styles(document: DocumentObject) -> None:
    """Hide latent built-in styles so only the six project styles appear."""
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
        else:
            latent_style.hidden = True
            latent_style.quick_style = False
            latent_style.unhide_when_used = False
            latent_style.priority = 99


# =====================================================================
#  XML helpers
# =====================================================================


def _style_exists(document: DocumentObject, style_name: str) -> bool:
    try:
        document.styles[style_name]
    except KeyError:
        return False
    return True


def _ensure_child(parent, tag: str) -> OxmlElement:
    """Return existing child or create a new one."""
    child = parent.find(qn(tag))
    if child is None:
        child = OxmlElement(tag)
        parent.append(child)
    return child


def _replace_child(parent, tag: str, new_child: OxmlElement) -> None:
    """Remove existing child with *tag* and append *new_child*."""
    old = parent.find(qn(tag))
    if old is not None:
        parent.remove(old)
    parent.append(new_child)


def _set_or_replace_child(parent, tag: str, attribs: dict[str, str]) -> OxmlElement:
    """Ensure a single child element with the given attributes."""
    old = parent.find(qn(tag))
    if old is not None:
        parent.remove(old)
    child = OxmlElement(tag)
    for key, val in attribs.items():
        child.set(key, val)
    parent.append(child)
    return child


def _make_elem(tag: str, attribs: dict[str, str] | None = None) -> OxmlElement:
    """Create a standalone OxmlElement."""
    elem = OxmlElement(tag)
    if attribs:
        for key, val in attribs.items():
            elem.set(key, val)
    return elem


def _make_rfonts_char() -> OxmlElement:
    """Build the rFonts element used by all linked character styles."""
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:asciiTheme"), FONT_THEME_REF)
    fonts.set(qn("w:hAnsiTheme"), FONT_THEME_REF)
    fonts.set(qn("w:cs"), FONT_FAMILY_CS)
    return fonts
