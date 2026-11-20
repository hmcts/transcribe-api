"""Word templates uploaded in the admin area (DIAAT-344).

A jurisdiction's template is an ordinary Word document whose fill-in boxes are content
controls (``w:sdt``). Uploading it lists its boxes; the product team links each box to
a field (its type, whether it is required, its options) and judges then fill those
fields on the Hearing Details form. No code change or release is needed.

- A box is known by its tag (Word's internal name), else its title (alias); several
  boxes with the same tag are one field and are all filled with its value. The title
  is the field's starting label.
- Fields can only be the template's boxes: none can be added, and every box needs one.
- At least one box takes the dictated text: each is a section (Background, Evidence...)
  the judge dictates into, with the notes under that section's heading. "Filled by
  BenchNotes" boxes take the jurisdiction's name or the year, or the wording its Legal
  Text Manager keeps (Document Content) for the judge's answers: the legal framework of
  the legal issues chosen, the description of the hearing type, the anonymity reasons
  and order. The judge is not asked for them.
- Legal issues, hearing type and anonymity are answered from the Document Content
  options; the legal issues box lists the chosen issues' questions, (a), (b)..., as
  the built-in IAC decision did.
- A question can be asked only when another's answer is one of those the admin chose
  (show_when): "Specify location" only after "Other", say. A question not asked is not
  answered, and its box is as if left empty.
- A text question can start with the judge's name or title (prefill), which they can
  change; the web app fills it in, from who is signed in.
- A box left empty (an optional field) prints its "when left empty" text if the admin
  set one; otherwise it is removed with its paragraph when every box in that paragraph
  is empty, so an unused party or panel member leaves no line behind.

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
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree

from docx import Document
from docx.oxml.ns import qn
from pydantic import BaseModel, ConfigDict, Field, field_validator

from transcribe_api.documents.lib.markdown_to_blocks import markdown_to_blocks
from transcribe_api.documents.lib.models import ContentBlock, Heading, Paragraph
from transcribe_api.documents.notes_parser import parse_notes_into_sections
from transcribe_api.documents.word_document import BoxParagraph, WordDocument

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


_TABLE_PARTS = frozenset({qn("w:tbl"), qn("w:tr"), qn("w:tc")})


def _unfillable(sdt: Any) -> str | None:
    """Why BenchNotes cannot fill a box, or None. It writes the value into the box's
    paragraphs or runs (WordDocument.set_content_control_text): a box around a table,
    row or cell, or with neither, would keep its placeholder."""
    content = sdt.find(qn("w:sdtContent"))
    children = {child.tag for child in content} if content is not None else set()
    if children & _TABLE_PARTS:
        return "holds a table, row or cell, which BenchNotes cannot fill. Put the box inside the cell instead."
    if not children & {qn("w:p"), qn("w:r")}:
        return "has no text for BenchNotes to replace. Type placeholder text in it in Word."
    return None


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
        unfillable = _unfillable(sdt)
        if unfillable:
            raise TemplateFileError(f"The box “{key}” {unfillable}")
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
    "hearing_type",
    "anonymity",
    "benchnotes",
    "dictated",
)
FieldType = Literal[
    "text",
    "date",
    "dropdown",
    "multi_select",
    "yes_no",
    "number",
    "legal_issues",
    "hearing_type",
    "anonymity",
    "benchnotes",
    "dictated",
]
LIST_TYPES = ("dropdown", "multi_select")
# Answered from the jurisdiction's Document Content, not options the admin lists: one
# box each at most, as the wording BenchNotes fills from them follows that one answer.
CONTENT_TYPES = ("legal_issues", "hearing_type", "anonymity")
# Not asked of the judge: BenchNotes fills them.
NOT_ASKED = ("benchnotes", "dictated")
BenchNotesSource = Literal[
    "jurisdiction",
    "year",
    "legal_framework",
    "hearing_description",
    "anonymity_reasons",
    "anonymity_order_heading",
    "anonymity_order",
]
# The answer each source's wording follows.
SOURCE_FOLLOWS: dict[str, str] = {
    "legal_framework": "legal_issues",
    "hearing_description": "hearing_type",
    "anonymity_reasons": "anonymity",
    "anonymity_order_heading": "anonymity",
    "anonymity_order": "anonymity",
}
_TYPE_NAMES = {"legal_issues": "Legal issues", "hearing_type": "Hearing type", "anonymity": "Anonymity"}
# Questions with set answers, which another question can be asked after.
CHOICE_TYPES = ("dropdown", "multi_select", "yes_no", *CONTENT_TYPES)
# A yes/no question's answers, as a show_when names them.
YES_NO_ANSWERS = ("yes", "no")
# What a text question can start with, for the judge to change.
Prefill = Literal["judge_name", "judge_title"]
MAX_TEXT_LENGTH = 5000


class ShowWhen(BaseModel):
    """Ask a question only when the question ``tag`` was answered with one of
    ``answers``: options, Document Content ids, or "yes"/"no"."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tag: str = Field(min_length=1, max_length=200)
    answers: list[str] = Field(default_factory=list, max_length=200)


# TemplateField's settings added after its first release.
_NEWER_SETTINGS = ("show_when", "empty_text", "prefill")


class TemplateField(BaseModel):
    """What fills one box: as stored with the template and edited in the admin area."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tag: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    type: FieldType = "text"
    required: bool = True
    options: list[str] = Field(default_factory=list, max_length=200)
    source: BenchNotesSource | None = None
    show_when: ShowWhen | None = None
    # Printed in the box when the judge leaves it empty (or is not asked).
    empty_text: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    prefill: Prefill | None = None

    @field_validator("empty_text")
    @classmethod
    def _clean_empty_text(cls, text: str | None) -> str | None:
        return text or None

    def to_record(self) -> dict[str, Any]:
        """As stored with the template: the newer settings only once set, so a pod still
        running a release from before them (which refuses keys it does not know) can
        read a template saved meanwhile."""
        return self.model_dump(exclude={k for k in _NEWER_SETTINGS if getattr(self, k) is None})

    @field_validator("options")
    @classmethod
    def _clean_options(cls, options: list[str]) -> list[str]:
        cleaned = [o.strip() for o in options]
        if any(not o or len(o) > 200 for o in cleaned):
            msg = "Each option must be 1 to 200 characters."
            raise ValueError(msg)
        return cleaned


# Tags (letters only, lower case) whose box BenchNotes fills from Document Content.
_PROPOSED_SOURCES: dict[str, BenchNotesSource] = {
    "legalframework": "legal_framework",
    "legalframeworkcontent": "legal_framework",
    "hearingdescription": "hearing_description",
    "anonymityreasons": "anonymity_reasons",
    "anonymityreasonstext": "anonymity_reasons",
    "anonymityorderheading": "anonymity_order_heading",
    "anonymityorderheader": "anonymity_order_heading",
    "anonymityreasonsheading": "anonymity_order_heading",
    "anonymityordertext": "anonymity_order",
}
_PROPOSED_SECTIONS = ("background", "evidence", "facts", "findingsoffact", "findingoffact")


def _proposed_type(box: Box) -> tuple[FieldType, BenchNotesSource | None]:
    key = re.sub(r"[^a-z]", "", box.key.lower())
    if box.kind == "dropdown":
        return "dropdown", None
    if box.kind == "date":
        return "date", None
    if any(word in key for word in ("judgmenttext", "judgementtext", "transcript", "dictat")) or (
        key in _PROPOSED_SECTIONS
    ):
        return "dictated", None
    if key == "jurisdiction":
        return "benchnotes", "jurisdiction"
    if key in ("year", "yearofpublication", "publicationyear"):
        return "benchnotes", "year"
    if key in _PROPOSED_SOURCES:
        return "benchnotes", _PROPOSED_SOURCES[key]
    if key in ("legalissues", "issues", "issueslist"):
        return "legal_issues", None
    if key in ("hearingtype", "hearingtitle"):
        return "hearing_type", None
    if key in ("anonymity", "anonymityhead", "anonymityorder"):
        return "anonymity", None
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
        problems.append("Link at least one box to the dictated text (the judgment).")
    first_named: dict[str, str | None] = {}
    for label in dictated:
        name = label.strip().lower()
        if first_named.get(name) is not None:
            problems.append(
                f"Two dictated boxes are both called “{first_named[name]}”: judges pick a section by its name,"
                " so give each its own."
            )
            first_named[name] = None  # said once
        elif name not in first_named:
            first_named[name] = label.strip()
    for kind in CONTENT_TYPES:
        linked = [f.label for f in fields if f.type == kind]
        if len(linked) > 1:
            problems.append(f"Only one box can be {_TYPE_NAMES[kind]}: " + ", ".join(f"“{d}”" for d in linked) + ".")
    problems.extend(_show_when_problems(by_tag, fields))
    types = {f.type for f in fields}
    for f in fields:
        follows = SOURCE_FOLLOWS.get(f.source or "") if f.type == "benchnotes" else None
        if follows and follows not in types:
            problems.append(
                f"“{f.label}” follows the judge's {_TYPE_NAMES[follows].lower()} answer: link a box to"
                f" {_TYPE_NAMES[follows]} too."
            )
    return problems


def _rule_answers(f: TemplateField) -> list[str] | None:
    """The answers a show_when can name for a question; None for Document Content ones,
    which its Legal Text Manager can change at any time."""
    if f.type in LIST_TYPES:
        return f.options
    if f.type == "yes_no":
        return list(YES_NO_ANSWERS)
    return None


def _show_when_problems(by_tag: dict[str, TemplateField], fields: list[TemplateField]) -> list[str]:
    problems: list[str] = []
    for f in fields:
        rule = f.show_when
        if rule is None or f.type in NOT_ASKED:
            continue
        after = by_tag.get(rule.tag)
        if after is None or after.tag == f.tag or after.type not in CHOICE_TYPES:
            problems.append(f"“{f.label}” can only be asked after a question with set answers: choose another.")
            continue
        allowed = _rule_answers(after)
        if not rule.answers:
            problems.append(f"Choose which answers to “{after.label}” ask “{f.label}”.")
        elif allowed is not None and any(a not in allowed for a in rule.answers):
            problems.append(f"“{f.label}” is asked after an answer “{after.label}” no longer has: choose again.")
        seen = {f.tag}
        while after is not None and after.show_when is not None:
            if after.tag in seen:
                problems.append(f"“{f.label}” waits on a question that waits on it: one of them must be asked first.")
                break
            seen.add(after.tag)
            after = by_tag.get(after.show_when.tag)
    return problems


def normalise_fields(fields: list[TemplateField]) -> list[TemplateField]:
    """Drop settings that do not apply to a field's type (options of a text field...)."""
    return [
        f.model_copy(
            update={
                "options": f.options if f.type in LIST_TYPES else [],
                "source": f.source if f.type == "benchnotes" else None,
                "required": f.required if f.type not in NOT_ASKED else False,
                "show_when": f.show_when if f.type not in NOT_ASKED else None,
                "empty_text": f.empty_text if f.type not in NOT_ASKED else None,
                "prefill": f.prefill if f.type == "text" else None,
            }
        )
        for f in fields
    ]


# The judge's values ----------------------------------------------------------------

Value = str | bool | list[str] | None


def _empty(value: Value) -> bool:
    return value is None or value == [] or (isinstance(value, str) and not value.strip())


def _answer_names(value: Value) -> set[str]:
    """An answer as a show_when names it."""
    if isinstance(value, bool):
        return {YES_NO_ANSWERS[0] if value else YES_NO_ANSWERS[1]}
    if isinstance(value, list):
        return {v for v in value if isinstance(v, str)}
    return {value.strip()} if isinstance(value, str) else set()


def hidden_fields(fields: list[TemplateField], values: dict[str, Value]) -> set[str]:
    """The questions not asked for these answers: one whose show_when answer was not
    given, or that is asked after a question not asked itself."""
    by_tag = {f.tag: f for f in fields}

    def asked(f: TemplateField, seen: frozenset[str]) -> bool:
        rule = f.show_when
        if rule is None:
            return True
        after = by_tag.get(rule.tag)
        if after is None or f.tag in seen or not asked(after, seen | {f.tag}):
            return False
        return bool(_answer_names(values.get(rule.tag)) & set(rule.answers))

    return {f.tag for f in fields if f.type not in NOT_ASKED and not asked(f, frozenset())}


def check_values(
    fields: list[TemplateField], values: dict[str, Value], choices: dict[str, list[str]]
) -> tuple[dict[str, Value], list[str]]:
    """The judge's values, checked against the fields: (the values kept, problems).
    Only fields the judge is asked for, given their other answers: a value for one not
    asked is dropped. ``choices`` are the jurisdiction's options for each CONTENT_TYPES
    field (legal frameworks, hearing types, anonymity), by type."""
    asked = {f.tag: f for f in fields if f.type not in NOT_ASKED}
    problems = [f"“{tag}” is not a field of this template." for tag in values if tag not in asked]
    hidden = hidden_fields(fields, values)
    kept: dict[str, Value] = {}
    for tag, f in asked.items():
        if tag in hidden:
            continue
        value = values.get(tag)
        if _empty(value):
            if f.required:
                problems.append(f"“{f.label}” is required.")
            continue
        problem = _value_problem(f, value, choices.get(f.type, []))
        if problem:
            problems.append(problem)
        else:
            kept[tag] = value.strip() if isinstance(value, str) else value
    return kept, problems


def _value_problem(f: TemplateField, value: Value, content_choices: list[str]) -> str | None:
    label = f"“{f.label}”"
    if f.type in ("multi_select", "legal_issues"):
        allowed = f.options if f.type == "multi_select" else content_choices
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
    if f.type in ("hearing_type", "anonymity"):
        return None if text in content_choices else f"{label} has a choice that is not one of its options."
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


DELETE_MARKER = "[DELETE]"
BoxContent = str | list[BoxParagraph]


@dataclass
class SectionText:
    """One dictated section: what was dictated into it, then the judge's notes under its
    heading."""

    transcript: list[str] = field(default_factory=list)
    notes: list[ContentBlock] = field(default_factory=list)


@dataclass
class FillContext:
    """What BenchNotes itself fills in."""

    jurisdiction_name: str
    year: int
    # Each dictated box's section, by its tag.
    sections: dict[str, SectionText] = field(default_factory=dict)
    # The jurisdiction's Document Content (hearing types, legal frameworks, anonymity).
    content: dict[str, Any] = field(default_factory=dict)


def legal_framework_options(content: dict[str, Any]) -> dict[str, str]:
    """Legal framework id -> title, in the Legal Text Manager's order."""
    frameworks = content.get("legal_frameworks") or []
    return {
        str(f["id"]): str(f.get("title") or f["id"])
        for f in sorted(frameworks, key=lambda f: f.get("rank", 0))
        if f.get("id")
    }


def hearing_type_options(content: dict[str, Any]) -> dict[str, str]:
    """Hearing type id -> title."""
    return {str(h["id"]): str(h.get("title") or h["id"]) for h in content.get("hearing_types") or [] if h.get("id")}


def anonymity_options(content: dict[str, Any]) -> dict[str, str]:
    """Anonymity status id -> what the judge picks: its short answer ("Yes", "No"...)."""
    return {
        str(a["id"]): str(a.get("identifier") or a.get("title") or a["id"])
        for a in (content.get("anonymity") or {}).get("statuses") or []
        if a.get("id")
    }


def content_choices(content: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Each CONTENT_TYPES field's options, id -> label."""
    return {
        "legal_issues": legal_framework_options(content),
        "hearing_type": hearing_type_options(content),
        "anonymity": anonymity_options(content),
    }


def _chosen_frameworks(content: dict[str, Any], chosen: list[str]) -> list[dict[str, Any]]:
    picked = set(chosen)
    return sorted(
        (f for f in content.get("legal_frameworks") or [] if str(f.get("id")) in picked),
        key=lambda f: f.get("rank", 999),
    )


def _entry(content: list[dict[str, Any]], chosen: Value) -> dict[str, Any]:
    return next((e for e in content if str(e.get("id")) == chosen), {})


def _wording(value: Any) -> str | None:
    """Document Content text, or None where it says to leave it out ([DELETE])."""
    text = str(value or "").strip()
    return None if not text or text == DELETE_MARKER else text


_ROMAN = ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x")
_LIST_INDENT_PT = 36.0


def _list_label(level: int, number: int) -> str:
    if level == 1:
        return f"({chr(ord('a') + (number - 1) % 26)})"
    if level == 2:
        return f"({_ROMAN[(number - 1) % len(_ROMAN)]})"
    return "•"


def box_paragraphs(blocks: list[ContentBlock]) -> list[BoxParagraph]:
    """Document blocks (from Markdown or the notes) as a box's paragraphs. The built-in
    decision's numbered lists, (a) then (i), are written out, as a template's own
    numbering would not match them."""
    paragraphs: list[BoxParagraph] = []
    counters: dict[int, int] = {}
    for block in blocks:
        if isinstance(block, Heading):
            counters.clear()
            paragraphs.append(BoxParagraph(text=block.text, bold=True, underline=block.underline))
        elif isinstance(block, Paragraph):
            level = block.list_level
            if level == 0:
                counters.clear()
                paragraphs.append(
                    BoxParagraph(
                        text=block.text,
                        label="•",
                        bold=block.bold,
                        italic=block.italic,
                        underline=block.underline,
                        left_indent_pt=block.left_indent_pt or _LIST_INDENT_PT,
                    )
                )
                continue
            if level is None or level < 0:
                counters.clear()
                paragraphs.append(
                    BoxParagraph(
                        text=block.text,
                        bold=block.bold,
                        italic=block.italic,
                        underline=block.underline,
                        left_indent_pt=block.left_indent_pt,
                    )
                )
                continue
            for deeper in [k for k in counters if k > level]:
                del counters[deeper]
            counters[level] = counters.get(level, 0) + 1
            paragraphs.append(
                BoxParagraph(
                    text=block.text,
                    label=_list_label(level, counters[level]),
                    bold=block.bold,
                    italic=block.italic,
                    underline=block.underline,
                    left_indent_pt=block.left_indent_pt or _LIST_INDENT_PT * level,
                )
            )
    return paragraphs


def _markdown(text: str | None) -> list[BoxParagraph] | None:
    paragraphs = box_paragraphs(markdown_to_blocks(text)) if text else []
    return paragraphs or None


def _section_paragraphs(f: TemplateField, section: SectionText | None) -> list[BoxParagraph]:
    """As the built-in decision's sections: what was dictated, then "Your notes" and the
    notes; a placeholder when there is neither."""
    transcript = [BoxParagraph(text=t.strip()) for t in (section.transcript if section else []) if t.strip()]
    notes = box_paragraphs(section.notes) if section else []
    if transcript and notes:
        return [*transcript, BoxParagraph(text="Your notes", bold=True), *notes]
    if transcript or notes:
        return transcript or notes
    return [BoxParagraph(text=f"[No {f.label.strip().lower()} content]")]


def _source_content(f: TemplateField, answers: dict[str, Value], context: FillContext) -> BoxContent | None:
    content = context.content
    if f.source == "jurisdiction":
        return context.jurisdiction_name
    if f.source == "year":
        return str(context.year)
    answer = answers.get(SOURCE_FOLLOWS.get(f.source or "", ""))
    if _empty(answer):
        return None
    if f.source == "legal_framework":
        chosen = _chosen_frameworks(content, answer)  # type: ignore[arg-type]
        return _markdown("\n\n".join(str(fw.get("content") or "") for fw in chosen).strip())
    if f.source == "hearing_description":
        return _markdown(_entry(content.get("hearing_types") or [], answer).get("hearing_description"))
    status = _entry((content.get("anonymity") or {}).get("statuses") or [], answer)
    if f.source == "anonymity_reasons":
        return _markdown(_wording(status.get("content")))
    if f.source == "anonymity_order_heading":
        return _wording(status.get("order_header"))
    return _markdown(_wording(status.get("order_text")))


def _answer_content(f: TemplateField, value: Value, context: FillContext) -> BoxContent | None:
    if f.type == "date":
        day = date.fromisoformat(str(value))
        return f"{day.day} {day:%B %Y}"
    if f.type == "yes_no":
        return "Yes" if value else "No"
    if f.type == "multi_select":
        return ", ".join(value)  # type: ignore[arg-type]
    if f.type == "legal_issues":
        issues = [
            str(issue).strip()
            for fw in _chosen_frameworks(context.content, value)  # type: ignore[arg-type]
            for issue in fw.get("issues") or []
            if str(issue).strip()
        ]
        return [
            BoxParagraph(text=issue, label=_list_label(1, n), left_indent_pt=_LIST_INDENT_PT)
            for n, issue in enumerate(issues, start=1)
        ] or None
    if f.type == "hearing_type":
        entry = _entry(context.content.get("hearing_types") or [], value)
        return str(entry.get("hearing_title") or entry.get("title") or value)
    if f.type == "anonymity":
        statuses = (context.content.get("anonymity") or {}).get("statuses") or []
        return _wording(_entry(statuses, value).get("header"))
    return str(value)


def box_text(fields: list[TemplateField], values: dict[str, Value], context: FillContext) -> dict[str, BoxContent]:
    """What goes in each box: text, or formatted paragraphs. A box with nothing (an
    optional field left empty with no "when left empty" text, or wording its answer
    leaves out) is absent."""
    answers = {f.type: values.get(f.tag) for f in fields if f.type in CONTENT_TYPES}
    texts: dict[str, BoxContent] = {}
    for f in fields:
        value = values.get(f.tag)
        content: BoxContent | None
        if f.type == "dictated":
            content = _section_paragraphs(f, context.sections.get(f.tag))
        elif f.type == "benchnotes":
            content = _source_content(f, answers, context)
        elif _empty(value):
            content = f.empty_text
        else:
            content = _answer_content(f, value, context)
        if isinstance(content, str) and not content.strip():
            content = None
        if content:
            texts[f.tag] = content
    return texts


def fill_template(data: bytes, texts: dict[str, BoxContent], output_path: Path) -> Path:
    """Fill the template's boxes and save it. An empty box is removed, and its paragraph
    too when every box in that paragraph is empty."""
    doc = WordDocument(document=Document(io.BytesIO(data)))
    empty: list[Any] = []
    for sdt in _leaf_controls(doc):
        key = _control_key(sdt)
        text = texts.get(key or "")
        if isinstance(text, list):
            doc.set_content_control_blocks(sdt, text)
        elif text:
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


def dictated_sections(
    fields: list[TemplateField], transcripts: dict[str, list[str]], notes_html: str | None
) -> dict[str, SectionText]:
    """Each dictated box's section: what was dictated into it (by its tag) and the part
    of the notes under its name. Dictation for a section the template does not have (an
    older page's Background, Evidence or Facts) goes into its first section."""
    sections = [(f.tag, f.label) for f in fields if f.type == "dictated"]
    if not sections:
        return {}
    first = sections[0][0]
    by_tag: dict[str, list[str]] = {tag: [] for tag, _ in sections}
    for tag, texts in transcripts.items():
        by_tag[tag if tag in by_tag else first].extend(texts)
    notes = parse_notes_into_sections(notes_html or "", sections)
    return {tag: SectionText(transcript=by_tag[tag], notes=list(notes.get(tag, []))) for tag, _ in sections}
