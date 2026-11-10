"""Word templates for the DIAAT-344 tests, made in code: paragraphs of text and fill-in
boxes (content controls), as a template owner makes them in Word."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls


@dataclass
class Sdt:
    """One fill-in box: its tag (None: none), title, and kind."""

    tag: str | None
    alias: str | None = None
    kind: str = "text"  # text, dropdown, date, checkbox, picture
    options: list[str] = field(default_factory=list)
    placeholder: str = "Click here"


def _sdt_xml(box: Sdt) -> str:
    props = []
    if box.alias:
        props.append(f"<w:alias w:val={quoteattr(box.alias)}/>")
    if box.tag:
        props.append(f"<w:tag w:val={quoteattr(box.tag)}/>")
    if box.kind == "dropdown":
        items = "".join(
            f"<w:listItem w:displayText={quoteattr(o)} w:value={quoteattr(o)}/>"
            for o in ["Choose an item.", *box.options]
        )
        props.append(f"<w:dropDownList>{items}</w:dropDownList>")
    elif box.kind == "date":
        props.append('<w:date><w:dateFormat w:val="dd/MM/yyyy"/></w:date>')
    elif box.kind == "picture":
        props.append("<w:picture/>")
    elif box.kind == "checkbox":
        props.append('<w14:checkbox xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"/>')
    return (
        f"<w:sdt {nsdecls('w')}><w:sdtPr>{''.join(props)}</w:sdtPr>"
        f"<w:sdtContent><w:r><w:t>{box.placeholder}</w:t></w:r></w:sdtContent></w:sdt>"
    )


def make_template(paragraphs: list[list[str | Sdt]], header: list[str | Sdt] | None = None) -> bytes:
    """A .docx: each paragraph is text and boxes in order; ``header`` the same, once."""
    document = Document()
    for parts in paragraphs:
        paragraph = document.add_paragraph()
        _fill(paragraph, parts)
    if header is not None:
        _fill(document.sections[0].header.paragraphs[0], header)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _fill(paragraph, parts: list[str | Sdt]) -> None:
    for part in parts:
        if isinstance(part, str):
            paragraph.add_run(part)
        else:
            paragraph._p.append(parse_xml(_sdt_xml(part)))


def with_part(data: bytes, name: str, content: bytes) -> bytes:
    """``data`` with one more part (a macro, say)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr(name, content)
    return out.getvalue()


def replace_part(data: bytes, name: str, transform) -> bytes:
    """``data`` with one part changed by ``transform(bytes) -> bytes``."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for item in source.infolist():
            content = source.read(item.filename)
            target.writestr(item, transform(content) if item.filename == name else content)
    return out.getvalue()


def document_text(data: bytes, part: str = "word/document.xml") -> list[str]:
    """Each non-empty paragraph's text (boxes included), in order."""
    from lxml import etree

    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(data)).read(part))
    texts = []
    for p in root.iter(f"{{{w}}}p"):
        nodes = p.xpath(".//w:t/text() | .//w:br", namespaces={"w": w})
        text = "".join(n if isinstance(n, str) else "\n" for n in nodes)
        if text.strip():
            texts.append(text)
    return texts


def header_part(data: bytes) -> str:
    names = [n for n in zipfile.ZipFile(io.BytesIO(data)).namelist() if n.startswith("word/header")]
    return names[0]


# A small judgment template like the generic one the product team uploads.
JUDGMENT = [
    ["Jurisdiction: ", Sdt("Jurisdiction")],
    ["Case Number: ", Sdt("Reference")],
    ["Heard on ", Sdt("HearingDate", "Hearing date", kind="date")],
    [Sdt("Party1", "Party1Name"), " (", Sdt("Party1Title", kind="dropdown", options=["Claimant", "Respondent"]), ")"],
    [Sdt("Party2", "Party2Name"), " (", Sdt("Party2Title", kind="dropdown", options=["Claimant", "Respondent"]), ")"],
    ["For the ", Sdt("Party2Title", kind="dropdown", options=["Claimant", "Respondent"]), ": ", Sdt("Party2Rep")],
    [Sdt("JudgmentText", "Judgment text")],
    ["© Crown copyright ", Sdt("Year")],
]
