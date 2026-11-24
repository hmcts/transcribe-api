"""Word templates uploaded in the admin area (DIAAT-344).

A jurisdiction's template is an ordinary Word document whose fill-in boxes are content
controls (``w:sdt``). Uploading it lists its boxes; the product team links each box to
a field (its type, whether it is required, its options) and judges then fill those
fields on the Hearing Details form. No code change or release is needed.

- A box is known by its tag (Word's internal name), else its title (alias); several
  boxes with the same tag are one field and are all filled with its value. The title
  is the field's starting label.
- Fields can only be the template's boxes: none can be added, and every box needs one.
- At least one box takes the dictated text. Each such box is a dictation section
  (DIAAT-416), named by its label: with several, the judge chooses the section they are
  dictating, and each box gets only its own section's dictation. The judge's typed
  notes follow the dictation in the last such box (in the admin's field order); with
  one box, or a page that sends no sections, everything goes in the first, as before.
  "Filled by BenchNotes" boxes take the jurisdiction's name or the year, and the judge
  is not asked for them.
- A "Filled by BenchNotes" box can take text depending on an answer (DIAAT-411), as the
  IAC's decision adds its rule 28 paragraphs: each of its rules prints its text when a
  question of the template has a given answer, in the admin's order, with its
  paragraphs, lists and bold kept. With none applying, the box and its line go.
  A rule's text can hold the judge's answers (DIAAT-412): ``{{Tag}}`` prints the answer to
  that question as its own box would, as plain text. An empty answer prints nothing, and
  takes a comma next to it, so "{{RepName}}, Counsel" with no name is "Counsel".
- A text field can start with the signed-in judge's name (DIAAT-340), as the IAC's form
  does for its first judge; the judge can change it.
- A "Filled by BenchNotes" box can take the legal framework text (DIAAT-415): the text the
  jurisdiction's Legal Text Manager keeps for each legal framework the judge chose in the
  template's Legal issues question, in the Legal Text Manager's order, each once, with its
  paragraphs, lists and bold kept. Nothing about any one jurisdiction is in the code.
- A box left empty (an optional field, a section with nothing dictated, or legal
  framework text with no framework chosen) is removed with its paragraph when every box in that paragraph is empty, so an unused
  party or panel member leaves no line behind. A legal framework box can have "when left
  empty" text instead.

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

from transcribe_api.documents.lib.markdown_to_blocks import markdown_to_blocks
from transcribe_api.documents.lib.models import ContentBlock, Heading, Paragraph, TextRun
from transcribe_api.documents.word_document import BoxParagraph, BoxRun, WordDocument

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
    "benchnotes",
    "dictated",
)
FieldType = Literal[
    "text", "date", "dropdown", "multi_select", "yes_no", "number", "legal_issues", "benchnotes", "dictated"
]
LIST_TYPES = ("dropdown", "multi_select")
# Not asked of the judge: BenchNotes fills them.
NOT_ASKED = ("benchnotes", "dictated")
BenchNotesSource = Literal["jurisdiction", "year", "legal_framework", "answer_text"]
# The questions text can depend on (DIAAT-411): those with set answers.
ANSWER_TYPES = ("dropdown", "multi_select", "yes_no")
YES_NO_ANSWERS = ("yes", "no")
# What a text field's answer starts as on the judge's form (DIAAT-340).
Prefill = Literal["judge_name"]


class AnswerRule(BaseModel):
    """DIAAT-411: print ``text`` (Markdown) when the question ``field`` (a tag of the same
    template) has the answer ``answer``: one of its options, or "yes" or "no"."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Empty until the admin chooses them: saving then says what is missing.
    field: str = Field(default="", max_length=200)
    answer: str = Field(default="", max_length=200)
    text: str = Field(default="", max_length=10000)


class TemplateField(BaseModel):
    """What fills one box: as stored with the template and edited in the admin area."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tag: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    type: FieldType = "text"
    required: bool = True
    options: list[str] = Field(default_factory=list, max_length=200)
    source: BenchNotesSource | None = None
    # Printed when a legal framework box has nothing to print (DIAAT-415).
    empty_text: str | None = Field(default=None, max_length=2000)
    # Filled in on the judge's form before they answer: they can change it (DIAAT-340).
    prefill: Prefill | None = None
    # A "text depending on an answer" box's rules, in the order printed (DIAAT-411).
    rules: list[AnswerRule] | None = Field(default=None, max_length=50)

    def stored(self) -> dict[str, Any]:
        """As kept with the template: empty_text, prefill and rules only when set, so pods
        from before them (extra="forbid") still read every template that does not use
        them, and its revision (DIAAT-339) stays the same."""
        unset = {name for name in ("empty_text", "prefill", "rules") if getattr(self, name) is None}
        return self.model_dump(exclude=unset)

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
    if key in ("legalframework", "legalframeworks", "legalframeworkcontent", "legalframeworktext"):
        return "benchnotes", "legal_framework"
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
        if _is_legal_framework(f) and not any(o.type == "legal_issues" for o in fields):
            problems.append(
                f"“{f.label}” prints the text of the legal frameworks the judge chooses: link a box"
                " to a Legal issues question for them to choose from."
            )
        if _is_answer_text(f):
            problems.extend(_rule_problems(f, fields))
    dictated = [f.label for f in fields if f.type == "dictated"]
    if not dictated:
        problems.append("Link at least one box to the dictated text.")
    names = [d.strip().casefold() for d in dictated]
    repeated = list(dict.fromkeys(d for d, n in zip(dictated, names, strict=True) if names.count(n) > 1))
    if repeated:
        problems.append(
            "Each box for the dictated text is a section judges choose by its name, so each needs"
            " its own: " + ", ".join(f"“{d}”" for d in repeated) + "."
        )
    return problems


def normalise_fields(fields: list[TemplateField]) -> list[TemplateField]:
    """Drop settings that do not apply to a field's type (options of a text field...)."""
    return [
        f.model_copy(
            update={
                "options": f.options if f.type in LIST_TYPES else [],
                "source": f.source if f.type == "benchnotes" else None,
                "required": f.required if f.type not in NOT_ASKED else False,
                "empty_text": ((f.empty_text or "").strip() or None) if _is_legal_framework(f) else None,
                "prefill": f.prefill if f.type == "text" else None,
                "rules": f.rules if _is_answer_text(f) else None,
            }
        )
        for f in fields
    ]


def _is_legal_framework(f: TemplateField) -> bool:
    return f.type == "benchnotes" and f.source == "legal_framework"


def _is_answer_text(f: TemplateField) -> bool:
    return f.type == "benchnotes" and f.source == "answer_text"


def _rule_problems(f: TemplateField, fields: list[TemplateField]) -> list[str]:
    """What stops a "text depending on an answer" box's rules being saved."""
    if not f.rules:
        return [f"Add a rule for when BenchNotes fills “{f.label}”."]
    by_tag = {o.tag: o for o in fields}
    asked = {o.tag for o in fields if o.type not in NOT_ASKED}
    problems: list[str] = []
    for number, rule in enumerate(f.rules, start=1):
        which = f"rule {number} of “{f.label}”"
        rule_name = f"Rule {number} of “{f.label}”"
        question = by_tag.get(rule.field)
        if not rule.field:
            problems.append(f"Choose the question {which} depends on.")
        elif question is None or question.type not in ANSWER_TYPES:
            problems.append(f"{rule_name} depends on a question that is not a dropdown, list or yes/no question.")
        elif not rule.answer:
            problems.append(f"Choose the answer of “{question.label}” {which} depends on.")
        elif rule.answer not in (YES_NO_ANSWERS if question.type == "yes_no" else question.options):
            problems.append(f"{rule_name} depends on an answer “{question.label}” no longer has: choose another.")
        if not rule.text:
            problems.append(f"{rule_name} has no text to print.")
        for unknown in dict.fromkeys(n for n in PLACEHOLDER.findall(rule.text) if n not in asked):
            problems.append(f"{rule_name} prints the answer to “{unknown}”, which is not a question the judge answers.")
    return problems


def _has_answer(question: TemplateField, value: Value, answer: str) -> bool:
    if question.type == "yes_no":
        return isinstance(value, bool) and value == (answer == "yes")
    if question.type == "multi_select":
        return isinstance(value, list) and answer in value
    return value == answer


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
    # Dictated box tag -> its text (DIAAT-416: each section's own).
    dictated_texts: dict[str, str]
    # Legal framework id -> title, in the jurisdiction's order.
    legal_issue_titles: dict[str, str] = field(default_factory=dict)
    # Legal framework id -> its text (Markdown), in the jurisdiction's order.
    legal_framework_texts: dict[str, str] = field(default_factory=dict)


# What goes in a box: plain text, or formatted paragraphs.
BoxContent = str | list[BoxParagraph]


def box_text(fields: list[TemplateField], values: dict[str, Value], context: FillContext) -> dict[str, BoxContent]:
    """What goes in each box; a box with nothing (an optional field left empty, or no
    legal framework chosen and no "when left empty" text) is absent."""
    texts: dict[str, BoxContent] = {}
    for f in fields:
        value = values.get(f.tag)
        text: str | None = None
        if _is_answer_text(f):
            paragraphs = _answer_text_paragraphs(f, fields, values, context)
            if paragraphs:
                texts[f.tag] = paragraphs
            continue
        if _is_legal_framework(f):
            paragraphs = _legal_framework_paragraphs(fields, values, context)
            if paragraphs:
                texts[f.tag] = paragraphs
                continue
            text = f.empty_text
        elif f.type == "dictated":
            text = context.dictated_texts.get(f.tag)
        elif f.type == "benchnotes":
            text = context.jurisdiction_name if f.source == "jurisdiction" else str(context.year)
        else:
            text = _printed_answer(f, value, context)
        if text and text.strip():
            texts[f.tag] = text
    return texts


def _printed_answer(f: TemplateField, value: Value, context: FillContext) -> str | None:
    """A question's answer as the document prints it; None when it was not answered."""
    if _empty(value):
        return None
    if f.type == "date":
        day = date.fromisoformat(str(value))
        return f"{day.day} {day:%B %Y}"
    if f.type == "yes_no":
        return "Yes" if value else "No"
    if f.type == "multi_select":
        return ", ".join(value)  # type: ignore[arg-type]
    if f.type == "legal_issues":
        chosen = set(value)  # type: ignore[arg-type]
        return ", ".join(t for i, t in context.legal_issue_titles.items() if i in chosen)
    return str(value)


# DIAAT-412: an answer in a rule's text, by its question's tag. Double braces, which
# legal text does not use, so text saved before them is never read as one.
PLACEHOLDER = re.compile(r"\{\{([^{}\n]+)\}\}")
# Where an empty answer was, while what surrounds it is tidied away: _GONE as it was,
# _GONE_WITH_COMMA once a comma next to it has gone too (one comma per answer).
_GONE = "\x00"
_GONE_WITH_COMMA = "\x01"
_ANY_GONE = f"[{_GONE}{_GONE_WITH_COMMA}]"
# Underlining around nothing but empty answers: markdown_to_blocks would underline the
# whole paragraph for it, so it goes before the text is read.
_EMPTY_UNDERLINE = re.compile(r"<u>(?:\s*\{\{([^{}\n]+)\}\})+\s*</u>", re.IGNORECASE)
# What goes around an empty answer, tidied until none is left: answers side by side as
# one; brackets around nothing else; one comma next to it (after it, else before it).
_TIDY_EMPTY = (
    (re.compile(rf"(?P<keep>{_ANY_GONE})\s*{_ANY_GONE}"), None),
    (re.compile(rf"\(\s*(?P<keep>{_ANY_GONE})\s*\)"), None),
    (re.compile(rf"\[\s*(?P<keep>{_ANY_GONE})\s*\]"), None),
    (re.compile(rf"(?P<keep>{_GONE})\s*,[ \t]*"), _GONE_WITH_COMMA),
    (re.compile(rf"[ \t]*,\s*(?P<keep>{_GONE})"), _GONE_WITH_COMMA),
)
_SPACED_GONE = re.compile(rf"([ \t]*){_ANY_GONE}([ \t]*)")

# A paragraph's text as characters, each with the run (bold, italic) it is in.
_Styled = list[tuple[str, bool, bool]]


def _without_empty_underlining(text: str, answers: dict[str, str]) -> str:
    def empty(match: re.Match[str]) -> bool:
        return all(answers.get(tag) == "" for tag in PLACEHOLDER.findall(match[0]))

    return _EMPTY_UNDERLINE.sub(lambda m: re.sub(r"</?u>", "", m[0], flags=re.IGNORECASE) if empty(m) else m[0], text)


def _replaced(chars: _Styled, match: re.Match[str], keep: str | None) -> _Styled:
    """``chars`` with the match's text replaced by its ``keep`` group (as ``keep``, if
    given), in the style of the first character it replaces."""
    kept = keep or match["keep"]
    _, bold, italic = chars[match.start()]
    return [*chars[: match.start()], *((c, bold, italic) for c in kept), *chars[match.end() :]]


def _tidied(chars: _Styled) -> _Styled:
    """The paragraph with each empty answer gone, and with it what would show it was there:
    brackets around nothing else, a comma, and spaces. One space is left between words;
    none at either end, before punctuation or a closing bracket, or after an opening one."""
    while True:
        text = "".join(c for c, _, _ in chars)
        for pattern, keep in _TIDY_EMPTY:
            match = pattern.search(text)
            if match:
                chars = _replaced(chars, match, keep)
                break
        else:
            break
    while True:
        text = "".join(c for c, _, _ in chars)
        match = _SPACED_GONE.search(text)
        if not match:
            return chars
        before, after = text[: match.start()], text[match.end() :]
        between_words = bool(before.strip() and after.strip() and after[0] not in ".,;:!?)]" and before[-1] not in "([")
        space: _Styled = []
        if between_words and (match[1] or match[2]):
            # One space, in the style of the first that was there.
            _, bold, italic = chars[match.start()] if match[1] else chars[match.end() - 1]
            space = [(" ", bold, italic)]
        chars = [*chars[: match.start()], *space, *chars[match.end() :]]


def _answered(block: ContentBlock, answers: dict[str, str]) -> ContentBlock | None:
    """The block with each placeholder of ``answers`` replaced by the answer as typed, on
    one line: filled in after the admin's Markdown is read, so nothing in an answer can
    format the document. An empty answer goes with what would show it was there (see
    _tidied); None when nothing is left of the block."""
    if not isinstance(block, Paragraph | Heading) or not any(PLACEHOLDER.search(r.text) for r in [block, *block.runs]):
        return block
    runs = block.runs or [TextRun(text=block.text, bold=block.bold, italic=getattr(block, "italic", False))]

    def fill(m: re.Match[str]) -> str:
        if m[1] not in answers:
            return m[0]
        return " ".join(answers[m[1]].split()) or _GONE

    chars: _Styled = [(c, r.bold, r.italic) for r in runs for c in PLACEHOLDER.sub(fill, r.text)]
    if any(c in (_GONE, _GONE_WITH_COMMA) for c, _, _ in chars):
        chars = _tidied(chars)
        if not "".join(c for c, _, _ in chars).strip():
            return None
    filled: list[TextRun] = []
    for c, bold, italic in chars:
        if filled and (filled[-1].bold, filled[-1].italic) == (bold, italic):
            filled[-1] = TextRun(text=filled[-1].text + c, bold=bold, italic=italic)
        else:
            filled.append(TextRun(text=c, bold=bold, italic=italic))
    text = "".join(r.text for r in filled)
    return block.model_copy(update={"text": text, "runs": filled if block.runs else []})


def answer_paragraphs(text: str, answers: dict[str, str]) -> list[BoxParagraph]:
    """A rule's text (Markdown) as paragraphs, with ``{{Tag}}`` replaced by the answers to
    those questions. An empty answer leaves no gap; placeholders naming no question stay.
    A list item left with nothing goes, its next paragraph, if any, taking its place."""
    blocks: list[ContentBlock] = []
    promote = False
    for block in markdown_to_blocks(_without_empty_underlining(text, answers)):
        answered = _answered(block, answers)
        if answered is None:
            promote = isinstance(block, Paragraph) and block.list_level is not None and not block.list_continued
            continue
        if promote and isinstance(answered, Paragraph) and answered.list_continued:
            answered = answered.model_copy(update={"list_continued": False})
        promote = False
        blocks.append(answered)
    return box_paragraphs(blocks)


def _answer_text_paragraphs(
    f: TemplateField, fields: list[TemplateField], values: dict[str, Value], context: FillContext
) -> list[BoxParagraph]:
    """The text of each of the box's rules whose answer the judge gave, in its order, with
    the answers it prints filled in."""
    by_tag = {o.tag: o for o in fields}
    paragraphs: list[BoxParagraph] = []
    for rule in f.rules or []:
        question = by_tag.get(rule.field)
        if question is None or not _has_answer(question, values.get(rule.field), rule.answer) or not rule.text.strip():
            continue
        answers = {
            tag: _printed_answer(by_tag[tag], values.get(tag), context) or ""
            for tag in PLACEHOLDER.findall(rule.text)
            if tag in by_tag and by_tag[tag].type not in NOT_ASKED
        }
        paragraphs.extend(answer_paragraphs(rule.text, answers))
    return paragraphs


def _legal_framework_paragraphs(
    fields: list[TemplateField], values: dict[str, Value], context: FillContext
) -> list[BoxParagraph]:
    """The text of every legal framework chosen in the template's Legal issues questions,
    in the jurisdiction's order and each once, however the judge chose them."""
    chosen: set[str] = set()
    for f in fields:
        value = values.get(f.tag)
        if f.type == "legal_issues" and isinstance(value, list):
            chosen.update(str(v) for v in value)
    paragraphs: list[BoxParagraph] = []
    for framework_id, text in context.legal_framework_texts.items():
        if framework_id in chosen and text.strip():
            paragraphs.extend(box_paragraphs(markdown_to_blocks(text)))
    return paragraphs


_ROMAN = (
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
    (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
)  # fmt: skip
_LIST_INDENT_PT = 36.0


def _alphabetic(number: int) -> str:
    """1 → a, 26 → z, 27 → aa: never back to a."""
    letters = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        letters = chr(ord("a") + rest) + letters
    return letters


def _roman(number: int) -> str:
    numeral = ""
    for value, letters in _ROMAN:
        count, number = divmod(number, value)
        numeral += letters * count
    return numeral


def _list_label(level: int, number: int) -> str:
    if level == 1:
        return f"({_alphabetic(number)})"
    if level == 2:
        return f"({_roman(number)})"
    return "•"


def box_paragraphs(blocks: list[ContentBlock]) -> list[BoxParagraph]:
    """Document blocks (from the Legal Text Manager's Markdown) as a box's paragraphs.
    Lists are written out, (a) then (i), or a bullet, as the template's own numbering
    would not match them; each list nested in another is indented further."""
    paragraphs: list[BoxParagraph] = []
    counters: dict[int, int] = {}
    for block in blocks:
        if isinstance(block, Heading):
            counters.clear()
            runs = tuple(BoxRun(r.text, r.bold, r.italic) for r in block.runs)
            paragraphs.append(BoxParagraph(text=block.text, bold=True, underline=block.underline, runs=runs))
            continue
        if not isinstance(block, Paragraph):
            continue
        level = block.list_level
        if level is None or level < 0:
            counters.clear()
            label, indent = None, block.left_indent_pt
        elif block.list_continued:
            # More of the same item: under its text, without a label of its own.
            label, indent = None, block.left_indent_pt or _LIST_INDENT_PT * ((block.list_depth or 0) + 1)
        else:
            ordered = block.list_ordered if block.list_ordered is not None else level > 0
            depth = block.list_depth if block.list_depth is not None else level
            for deeper in [k for k in counters if k > depth]:
                del counters[deeper]
            if ordered:
                counters[depth] = counters.get(depth, 0) + 1
                label = _list_label(level, counters[depth])
            else:
                counters.pop(depth, None)
                label = "•"
            indent = block.left_indent_pt or _LIST_INDENT_PT * (depth + 1)
        # Formatted within the line: the runs carry all of its bold and italic (a
        # quote's too), rather than one bold word making the whole paragraph bold.
        runs = tuple(BoxRun(r.text, r.bold, r.italic) for r in block.runs)
        paragraphs.append(
            BoxParagraph(
                text=block.text,
                label=label,
                bold=block.bold and not runs,
                italic=block.italic and not runs,
                underline=block.underline,
                left_indent_pt=indent,
                runs=runs,
            )
        )
    return paragraphs


def fill_template(data: bytes, texts: dict[str, BoxContent], output_path: Path) -> Path:
    """Fill the template's boxes and save it. An empty box is removed, and its paragraph
    too when every box in that paragraph is empty."""
    doc = WordDocument(document=Document(io.BytesIO(data)))
    empty: list[Any] = []
    for sdt in _leaf_controls(doc):
        key = _control_key(sdt)
        text = texts.get(key or "")
        if isinstance(text, list) and text:
            doc.set_content_control_blocks(sdt, text)
        elif isinstance(text, str) and text:
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


def dictation_sections(fields: list[TemplateField]) -> list[TemplateField]:
    """The template's dictation sections (DIAAT-416): its dictated boxes, in its order."""
    return [f for f in fields if f.type == "dictated"]


def sections_cover(fields: list[TemplateField], sections: dict[str, list[str]], transcript: list[str]) -> bool:
    """Whether the sections hold exactly what was dictated, in the template's order:
    nothing in the stored transcript is missing from the document, nor the reverse."""
    return [text for f in dictation_sections(fields) for text in sections.get(f.tag, [])] == transcript


def unknown_sections(fields: list[TemplateField], sections: dict[str, list[str]]) -> list[str]:
    """The sections sent that are none of the template's (its fields may have changed
    since the judge began): their dictation would have no box to go in."""
    tags = {f.tag for f in dictation_sections(fields)}
    return [tag for tag in sections if tag not in tags]


def dictated_texts(
    fields: list[TemplateField], sections: dict[str, list[str]] | None, transcript: list[str], notes_html: str | None
) -> dict[str, str]:
    """Each dictated box's text (DIAAT-416): its own section's dictation only, and the
    judge's typed notes after the last section's. With no sections (one box, or a page
    from before them), all of it, notes too, goes in the first box, as before sections."""
    tags = [f.tag for f in dictation_sections(fields)]
    if not tags:
        return {}
    if sections is None:
        return {tag: dictated_text(transcript, notes_html) if tag == tags[0] else "" for tag in tags}
    texts = {tag: dictated_text(sections.get(tag, []), None) for tag in tags}
    texts[tags[-1]] = dictated_text(sections.get(tags[-1], []), notes_html)
    return texts


def dictated_text(transcript: list[str], notes_html: str | None) -> str:
    """A dictated box's text: what was dictated, then the judge's own text."""
    paragraphs = [t.strip() for t in transcript if t and t.strip()]
    if notes_html:
        parser = _BlockText()
        parser.feed(notes_html)
        parser.close()
        paragraphs.extend(parser.paragraphs)
    return "\n\n".join(paragraphs)
