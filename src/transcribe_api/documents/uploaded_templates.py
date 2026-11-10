"""Word templates uploaded in the admin area (DIAAT-344).

A jurisdiction's template is an ordinary Word document whose fill-in boxes are content
controls (``w:sdt``). Uploading it lists its boxes; the product team links each box to
a field (its type, whether it is required, its options) and judges then fill those
fields on the Hearing Details form. No code change or release is needed.

- A box is known by its tag (Word's internal name), else its title (alias); several
  boxes with the same tag are one field and are all filled with its value. The title
  is the field's starting label.
- Fields can only be the template's boxes: none can be added, and every box needs one.
- Exactly one box takes the dictated text (the judgment). "Filled by BenchNotes" boxes
  take the jurisdiction's name or the year, and the judge is not asked for them.
- A box left empty (an optional field) is removed with its paragraph when every box in
  that paragraph is empty, so an unused party or panel member leaves no line behind.

An upload is checked before anything reads it: a size cap, a zip within limits, and no
macros, ActiveX, embedded objects, imported chunks or external links other than
hyperlinks, whether a relationship or a field that fetches content (INCLUDEPICTURE...).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree

from docx import Document
from docx.oxml.ns import qn
from pydantic import BaseModel, ConfigDict, Field, field_validator

from transcribe_api.documents.word_document import WordDocument

# The upload ------------------------------------------------------------------------

MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 30 * 1024 * 1024
MAX_ZIP_ENTRIES = 1000

_DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_HYPERLINK = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
_REJECTED_PARTS = re.compile(r"(vbaProject\.bin|vbaData\.xml|/activeX/|/embeddings/)", re.IGNORECASE)
_REL_TYPE_REJECTED = re.compile(r"/(aFChunk|oleObject|package|control|attachedTemplate)$")
_RELS_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_WORD_XML = re.compile(r"^word/.+\.xml$")
# Fields Word fills by fetching a file or address when it updates fields: the
# address is in the field's instruction, with no relationship to check.
_FETCHING_FIELDS = frozenset({"INCLUDEPICTURE", "INCLUDETEXT", "LINK", "DDE", "DDEAUTO", "IMPORT"})
_FIELD_NAME = re.compile(r"\s*([A-Za-z]+)")
_CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"


class TemplateFileError(ValueError):
    """The upload is not a Word template BenchNotes can use; the message says why, in
    plain words for the admin area."""


def check_template_file(data: bytes) -> None:
    """Refuse anything but a plain .docx within limits (TemplateFileError)."""
    if not data:
        raise TemplateFileError("The file is empty.")
    if len(data) > MAX_TEMPLATE_BYTES:
        raise TemplateFileError(f"The file is larger than {MAX_TEMPLATE_BYTES // (1024 * 1024)} MB.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
    except (zipfile.BadZipFile, ValueError):
        raise TemplateFileError("This is not a Word document (.docx).") from None
    if len(entries) > MAX_ZIP_ENTRIES or sum(e.file_size for e in entries) > MAX_UNCOMPRESSED_BYTES:
        raise TemplateFileError("The document is too large once unpacked.")
    names = {e.filename for e in entries}
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise TemplateFileError("This is not a Word document (.docx).")
    try:
        types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
        overrides = {
            o.get("PartName", "").lstrip("/"): o.get("ContentType", "") for o in types.iter(f"{_CT_NS}Override")
        }
        if overrides.get("word/document.xml") != _DOCX_MAIN:
            raise TemplateFileError("Save the template as a Word document (.docx), not a macro-enabled or .dotx file.")
        if any(_REJECTED_PARTS.search(name) for name in names):
            raise TemplateFileError("The document contains macros, ActiveX controls or embedded objects.")
        for name in names:
            if not name.endswith(".rels"):
                continue
            for rel in ElementTree.fromstring(archive.read(name)).iter(f"{_RELS_NS}Relationship"):
                kind = rel.get("Type", "")
                if _REL_TYPE_REJECTED.search(kind):
                    raise TemplateFileError("The document contains embedded or linked content.")
                # An external hyperlink is only followed when clicked; a "cid:" target
                # (an image pasted from an email) is never fetched. Anything else (an
                # image or file on the web or a network share) Word would fetch.
                target = rel.get("Target", "")
                if rel.get("TargetMode") == "External" and kind != _HYPERLINK and not target.startswith("cid:"):
                    raise TemplateFileError("The document links to an external file.")
        for name in sorted(n for n in names if _WORD_XML.match(n)):
            for code in _field_codes(ElementTree.fromstring(archive.read(name))):
                field_name = _FIELD_NAME.match(code)
                if field_name and field_name.group(1).upper() in _FETCHING_FIELDS:
                    raise TemplateFileError(
                        f"The document has a {field_name.group(1).upper()} field, which fetches content from"
                        " elsewhere. Remove the field in Word (Alt+F9 shows field codes)."
                    )
    except (ElementTree.ParseError, zipfile.BadZipFile):
        raise TemplateFileError("The document is damaged.") from None


def _field_codes(root: ElementTree.Element) -> list[str]:
    """Every field's instruction in a part: a simple field's (w:fldSimple), and a complex
    field's, whose text can be split over many w:instrText runs between its begin and
    end (nested fields each have their own)."""
    codes: list[str] = []
    open_fields: list[list[str]] = []
    for element in root.iter():
        if element.tag == f"{_W_NS}fldSimple":
            codes.append(element.get(f"{_W_NS}instr", ""))
        elif element.tag == f"{_W_NS}fldChar":
            kind = element.get(f"{_W_NS}fldCharType")
            if kind == "begin":
                open_fields.append([])
            elif kind == "end" and open_fields:
                codes.append("".join(open_fields.pop()))
        elif element.tag == f"{_W_NS}instrText" and open_fields:
            open_fields[-1].append(element.text or "")
    codes.extend("".join(parts) for parts in open_fields)  # a field never ended
    return codes


# The boxes -------------------------------------------------------------------------

BoxKind = Literal["text", "dropdown", "date"]


@dataclass
class Box:
    """One field's worth of boxes: every content control with the same key."""

    key: str
    label: str
    kind: BoxKind
    options: list[str] = field(default_factory=list)
    count: int = 1

    def to_record(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "kind": self.kind, "options": self.options, "count": self.count}

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Box:
        return cls(**record)


def _val(element: Any, name: str) -> str | None:
    found = element.find(qn(f"w:{name}"))
    value = found.get(qn("w:val")) if found is not None else None
    return value.strip() if value and value.strip() else None


def _is_prompt(text: str) -> bool:
    """Word's "Choose an item." style entries are prompts, not options."""
    return text.lower().startswith("choose")


def _leaf_controls(doc: WordDocument) -> list[Any]:
    """Content controls to fill: not a building block (a page number, contents), not
    one that only groups others."""
    leaves = []
    for sdt in doc.iter_content_control_elements():
        props = sdt.find(qn("w:sdtPr"))
        if props is not None and (props.find(qn("w:docPartObj")) is not None):
            continue
        if sdt.xpath(".//*[local-name()='sdtContent']//*[local-name()='sdt']"):
            continue
        leaves.append(sdt)
    return leaves


def _control_key(sdt: Any) -> str | None:
    props = sdt.find(qn("w:sdtPr"))
    if props is None:
        return None
    return _val(props, "tag") or _val(props, "alias")


def _shown_text(sdt: Any) -> str:
    text = "".join(sdt.xpath(".//*[local-name()='t']/text()")).strip()
    return text[:40] or "an empty box"


def find_boxes(data: bytes) -> list[Box]:
    """The template's fields, in document order (TemplateFileError when a box cannot
    be used). Call check_template_file first."""
    try:
        doc = WordDocument(document=Document(io.BytesIO(data)))
    except Exception:
        raise TemplateFileError("The document could not be read.") from None
    boxes: dict[str, Box] = {}
    for sdt in _leaf_controls(doc):
        props = sdt.find(qn("w:sdtPr"))
        key = _control_key(sdt)
        if key is None:
            raise TemplateFileError(
                f"A fill-in box has no tag or title (it shows “{_shown_text(sdt)}”). Give it a tag in Word."
            )
        if props.find(qn("w:picture")) is not None or props.find(qn("w14:checkbox")) is not None:
            raise TemplateFileError(f"The box “{key}” is a picture or tick box, which BenchNotes cannot fill yet.")
        kind: BoxKind = "text"
        options: list[str] = []
        choices = props.find(qn("w:dropDownList"))
        if choices is None:
            choices = props.find(qn("w:comboBox"))
        if choices is not None:
            kind = "dropdown"
            for item in choices.findall(qn("w:listItem")):
                text = (item.get(qn("w:displayText")) or item.get(qn("w:value")) or "").strip()
                if text and not _is_prompt(text) and text not in options:
                    options.append(text)
        elif props.find(qn("w:date")) is not None:
            kind = "date"
        if key in boxes:
            box = boxes[key]
            if box.kind != kind:
                raise TemplateFileError(f"The box “{key}” is used as both a {box.kind} and a {kind} box.")
            box.count += 1
            continue
        boxes[key] = Box(key=key, label=_val(props, "alias") or key, kind=kind, options=options)
    if not boxes:
        raise TemplateFileError("The document has no fill-in boxes (content controls).")
    return list(boxes.values())


# The fields ------------------------------------------------------------------------

FIELD_TYPES = (
    "text",
    "date",
    "dropdown",
    "multi_select",
    "yes_no",
    "number",
    "legal_issues",
    "benchnotes",
    "dictated",
)
FieldType = Literal[
    "text", "date", "dropdown", "multi_select", "yes_no", "number", "legal_issues", "benchnotes", "dictated"
]
LIST_TYPES = ("dropdown", "multi_select")
# Not asked of the judge: BenchNotes fills them.
NOT_ASKED = ("benchnotes", "dictated")
BenchNotesSource = Literal["jurisdiction", "year"]


class TemplateField(BaseModel):
    """What fills one box: as stored with the template and edited in the admin area."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tag: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    type: FieldType = "text"
    required: bool = True
    options: list[str] = Field(default_factory=list, max_length=200)
    source: BenchNotesSource | None = None

    @field_validator("options")
    @classmethod
    def _clean_options(cls, options: list[str]) -> list[str]:
        cleaned = [o.strip() for o in options]
        if any(not o or len(o) > 200 for o in cleaned):
            msg = "Each option must be 1 to 200 characters."
            raise ValueError(msg)
        return cleaned


def _proposed_type(box: Box) -> tuple[FieldType, BenchNotesSource | None]:
    key = re.sub(r"[^a-z]", "", box.key.lower())
    if box.kind == "dropdown":
        return "dropdown", None
    if box.kind == "date":
        return "date", None
    if any(word in key for word in ("judgmenttext", "judgementtext", "transcript", "dictat")):
        return "dictated", None
    if key == "jurisdiction":
        return "benchnotes", "jurisdiction"
    if key in ("year", "yearofpublication", "publicationyear"):
        return "benchnotes", "year"
    return "text", None


def propose_field(box: Box) -> TemplateField:
    """A box's starting field: its title as the label, a type from what Word says it is
    (or its tag), its options. The admin changes any of it."""
    kind, source = _proposed_type(box)
    return TemplateField(
        tag=box.key,
        label=box.label[:200],
        type=kind,
        required=kind not in NOT_ASKED,
        options=box.options if kind in LIST_TYPES else [],
        source=source,
    )


def merge_fields(boxes: list[Box], fields: list[TemplateField]) -> list[TemplateField]:
    """After a new file: the fields kept for the boxes it still has, in their order.
    A box new in this file has no field until the admin links one."""
    keys = {b.key for b in boxes}
    return [f for f in fields if f.tag in keys]


def field_problems(boxes: list[Box], fields: list[TemplateField]) -> list[str]:
    """What stops the fields being saved, in plain words; empty when they can be."""
    problems: list[str] = []
    by_tag: dict[str, TemplateField] = {}
    for f in fields:
        if f.tag in by_tag:
            problems.append(f"“{f.label}” is linked to the box {f.tag} more than once.")
        by_tag[f.tag] = f
    keys = {b.key for b in boxes}
    for box in boxes:
        if box.key not in by_tag:
            problems.append(f"The box “{box.label}” ({box.key}) has no field linked to it.")
    for f in fields:
        if f.tag not in keys:
            problems.append(f"“{f.label}” ({f.tag}) is not a box in the template: fields cannot be added.")
        if f.type in LIST_TYPES and not f.options:
            problems.append(f"“{f.label}” needs at least one option.")
        if f.type in LIST_TYPES and len(set(f.options)) != len(f.options):
            problems.append(f"“{f.label}” has the same option twice.")
        if f.type == "benchnotes" and f.source is None:
            problems.append(f"Choose what BenchNotes fills “{f.label}” with.")
    dictated = [f.label for f in fields if f.type == "dictated"]
    if not dictated:
        problems.append("Link one box to the dictated text (the judgment).")
    elif len(dictated) > 1:
        problems.append("Only one box can take the dictated text: " + ", ".join(f"“{d}”" for d in dictated) + ".")
    return problems


def normalise_fields(fields: list[TemplateField]) -> list[TemplateField]:
    """Drop settings that do not apply to a field's type (options of a text field...)."""
    return [
        f.model_copy(
            update={
                "options": f.options if f.type in LIST_TYPES else [],
                "source": f.source if f.type == "benchnotes" else None,
                "required": f.required if f.type not in NOT_ASKED else False,
            }
        )
        for f in fields
    ]


# The judge's values ----------------------------------------------------------------

MAX_TEXT_LENGTH = 5000
Value = str | bool | list[str] | None


def _empty(value: Value) -> bool:
    return value is None or value == [] or (isinstance(value, str) and not value.strip())


def check_values(
    fields: list[TemplateField], values: dict[str, Value], legal_issue_ids: list[str]
) -> tuple[dict[str, Value], list[str]]:
    """The judge's values, checked against the fields: (the values kept, problems).
    Only fields the judge is asked for; ``legal_issue_ids`` are the jurisdiction's."""
    asked = {f.tag: f for f in fields if f.type not in NOT_ASKED}
    problems = [f"“{tag}” is not a field of this template." for tag in values if tag not in asked]
    kept: dict[str, Value] = {}
    for tag, f in asked.items():
        value = values.get(tag)
        if _empty(value):
            if f.required:
                problems.append(f"“{f.label}” is required.")
            continue
        problem = _value_problem(f, value, legal_issue_ids)
        if problem:
            problems.append(problem)
        else:
            kept[tag] = value.strip() if isinstance(value, str) else value
    return kept, problems


def _value_problem(f: TemplateField, value: Value, legal_issue_ids: list[str]) -> str | None:
    label = f"“{f.label}”"
    if f.type in ("multi_select", "legal_issues"):
        allowed = f.options if f.type == "multi_select" else legal_issue_ids
        if not isinstance(value, list) or any(not isinstance(v, str) or v not in allowed for v in value):
            return f"{label} has a choice that is not one of its options."
        return None
    if f.type == "yes_no":
        return None if isinstance(value, bool) else f"{label} must be yes or no."
    if not isinstance(value, str):
        return f"{label} must be text."
    text = value.strip()
    if f.type == "dropdown":
        return None if text in f.options else f"{label} has a choice that is not one of its options."
    if f.type == "date":
        try:
            date.fromisoformat(text)
        except ValueError:
            return f"{label} must be a date."
        return None
    if f.type == "number":
        try:
            Decimal(text)
        except InvalidOperation:
            return f"{label} must be a number."
        return None
    return f"{label} is too long." if len(text) > MAX_TEXT_LENGTH else None


@dataclass
class FillContext:
    """What BenchNotes itself fills in."""

    jurisdiction_name: str
    year: int
    dictated_text: str
    # Legal framework id -> title, in the jurisdiction's order.
    legal_issue_titles: dict[str, str] = field(default_factory=dict)


def box_text(fields: list[TemplateField], values: dict[str, Value], context: FillContext) -> dict[str, str]:
    """The text for each box; a box with none (an optional field left empty) is absent."""
    texts: dict[str, str] = {}
    for f in fields:
        value = values.get(f.tag)
        text: str | None = None
        if f.type == "dictated":
            text = context.dictated_text
        elif f.type == "benchnotes":
            text = context.jurisdiction_name if f.source == "jurisdiction" else str(context.year)
        elif _empty(value):
            text = None
        elif f.type == "date":
            day = date.fromisoformat(str(value))
            text = f"{day.day} {day:%B %Y}"
        elif f.type == "yes_no":
            text = "Yes" if value else "No"
        elif f.type == "multi_select":
            text = ", ".join(value)  # type: ignore[arg-type]
        elif f.type == "legal_issues":
            chosen = set(value)  # type: ignore[arg-type]
            text = ", ".join(t for i, t in context.legal_issue_titles.items() if i in chosen)
        else:
            text = str(value)
        if text and text.strip():
            texts[f.tag] = text
    return texts


def fill_template(data: bytes, texts: dict[str, str], output_path: Path) -> Path:
    """Fill the template's boxes and save it. An empty box is removed, and its paragraph
    too when every box in that paragraph is empty."""
    doc = WordDocument(document=Document(io.BytesIO(data)))
    empty: list[Any] = []
    for sdt in _leaf_controls(doc):
        key = _control_key(sdt)
        text = texts.get(key or "")
        if text:
            doc.set_content_control_text(sdt, text)
        else:
            empty.append(sdt)
    empty_ids = {id(sdt) for sdt in empty}
    for sdt in empty:
        paragraph = next((a for a in sdt.iterancestors() if a.tag == qn("w:p")), None)
        if paragraph is None or paragraph.getparent() is None:
            parent = sdt.getparent()
            _remove(sdt)
            if parent is not None and parent.tag == qn("w:tc") and not parent.findall(qn("w:p")):
                parent.append(parent.makeelement(qn("w:p")))
            continue
        in_paragraph = [
            s
            for s in paragraph.iter(qn("w:sdt"))
            if not s.xpath(".//*[local-name()='sdtContent']//*[local-name()='sdt']")
        ]
        container = paragraph.getparent()
        last_in_cell = container.tag == qn("w:tc") and len(container.findall(qn("w:p"))) == 1
        if all(id(s) in empty_ids for s in in_paragraph):
            if last_in_cell:
                for child in list(paragraph):
                    if child.tag != qn("w:pPr"):
                        paragraph.remove(child)
            else:
                container.remove(paragraph)
        else:
            _remove(sdt)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)
    return output_path


def _remove(sdt: Any) -> None:
    parent = sdt.getparent()
    if parent is not None:
        parent.remove(sdt)


# The dictated text -----------------------------------------------------------------


class _BlockText(HTMLParser):
    """The editor's HTML as plain paragraphs: one per paragraph, heading or list item."""

    _BLOCKS = frozenset({"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"})

    def __init__(self) -> None:
        super().__init__()
        self.paragraphs: list[str] = []
        self._parts: list[str] = []
        self._bullet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCKS:
            self._flush()
            self._bullet = tag == "li"
        elif tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCKS:
            self._flush()

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def _flush(self) -> None:
        text = "".join(self._parts).strip()
        self._parts = []
        if text:
            self.paragraphs.append(f"• {text}" if self._bullet else text)

    def close(self) -> None:
        super().close()
        self._flush()


def dictated_text(transcript: list[str], notes_html: str | None) -> str:
    """The judgment box's text: what was dictated, then the judge's own text."""
    paragraphs = [t.strip() for t in transcript if t and t.strip()]
    if notes_html:
        parser = _BlockText()
        parser.feed(notes_html)
        parser.close()
        paragraphs.extend(parser.paragraphs)
    return "\n\n".join(paragraphs)
