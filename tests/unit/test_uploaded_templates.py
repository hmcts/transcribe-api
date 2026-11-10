"""DIAAT-344: a Word template uploaded in the admin area, its boxes, the fields linked to
them, and the document a judge's values make from it (documents/uploaded_templates.py)."""

from __future__ import annotations

import re

import pytest

from tests.unit.template_docx import JUDGMENT, Sdt, document_text, header_part, make_template, replace_part, with_part
from transcribe_api.documents import uploaded_templates as t

# The file -------------------------------------------------------------------------


def test_a_plain_word_document_is_accepted():
    t.check_template_file(make_template(JUDGMENT))


@pytest.mark.parametrize(
    ("data", "says"),
    [
        (b"", "empty"),
        (b"%PDF-1.7 not a zip", "not a Word document"),
        (b"x" * (t.MAX_TEMPLATE_BYTES + 1), "larger than 2 MB"),
    ],
    ids=["empty", "not-a-docx", "too-large"],
)
def test_a_file_that_is_not_a_usable_docx_is_refused(data, says):
    with pytest.raises(t.TemplateFileError, match=says):
        t.check_template_file(data)


def test_macros_and_embedded_objects_are_refused():
    template = make_template(JUDGMENT)
    for part in ("word/vbaProject.bin", "word/embeddings/oleObject1.bin", "word/activeX/activeX1.xml"):
        with pytest.raises(t.TemplateFileError, match="macros"):
            t.check_template_file(with_part(template, part, b"x"))


def test_a_macro_enabled_document_is_refused():
    macro_enabled = replace_part(
        make_template(JUDGMENT),
        "[Content_Types].xml",
        lambda xml: xml.replace(b"document.main+xml", b"document.macroEnabled.main+xml"),
    )
    with pytest.raises(t.TemplateFileError, match="macro-enabled"):
        t.check_template_file(macro_enabled)


@pytest.mark.parametrize(
    ("target", "refused"),
    [
        ("https://example.test/tracker.png", True),
        ("file:///\\\\server\\share\\x.png", True),
        ("cid:image002.png@01DB", False),
    ],
    ids=["web-image", "network-share", "pasted-from-email"],
)
def test_an_external_link_word_would_fetch_is_refused(target, refused):
    rel = (
        '<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"'
        f' Target="{target}" TargetMode="External"/></Relationships>'
    ).encode()
    linked = replace_part(
        make_template(JUDGMENT), "word/_rels/document.xml.rels", lambda xml: xml.replace(b"</Relationships>", rel)
    )
    if refused:
        with pytest.raises(t.TemplateFileError, match="external file"):
            t.check_template_file(linked)
    else:
        t.check_template_file(linked)


def _simple(instr: str) -> str:
    quoted = instr.replace('"', "&quot;")
    return f'<w:p><w:fldSimple w:instr="{quoted}"><w:r><w:t>x</w:t></w:r></w:fldSimple></w:p>'


def _complex(*parts: str, inner: str = "") -> str:
    """A field whose instruction is split over runs; ``inner`` a nested field's runs."""
    runs = "".join(f'<w:r><w:instrText xml:space="preserve">{p}</w:instrText></w:r>' for p in parts)
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f"{runs}{inner}"
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>x</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def _with_body_xml(xml: str, data: bytes | None = None) -> bytes:
    return replace_part(
        data or make_template(JUDGMENT),
        "word/document.xml",
        lambda doc: doc.replace(b"<w:sectPr", xml.encode() + b"<w:sectPr", 1),
    )


@pytest.mark.parametrize(
    ("xml", "field"),
    [
        (_simple(' INCLUDEPICTURE "https://example.test/x.png" \\d '), "INCLUDEPICTURE"),
        (f"<w:p>{_complex(' INCLUDE', 'TEXT &quot;\\\\server\\share\\a.docx&quot; ')}</w:p>", "INCLUDETEXT"),
        (_simple(' includepicture "https://example.test/x.png" '), "INCLUDEPICTURE"),
        (
            f"<w:p>{_complex(' IF 1 = 1 ', inner=_complex(' LINK Excel.Sheet.12 &quot;https://x/y.xlsx&quot; '))}</w:p>",
            "LINK",
        ),
        (_simple(' DDEAUTO cmd "/c calc" '), "DDEAUTO"),
    ],
    ids=["simple", "split-complex", "lower-case", "nested", "dde"],
)
def test_a_field_that_fetches_content_is_refused(xml, field):
    with pytest.raises(t.TemplateFileError, match=f"has a {field} field"):
        t.check_template_file(_with_body_xml(xml))


def test_a_fetching_field_in_the_header_is_refused_too():
    data = make_template(JUDGMENT, header=["Case Number: ", Sdt("Reference")])
    part = header_part(data)
    data = replace_part(
        data,
        part,
        lambda xml: xml.replace(b"</w:hdr>", _simple(" INCLUDEPICTURE https://x/y.png ").encode() + b"</w:hdr>"),
    )
    with pytest.raises(t.TemplateFileError, match="INCLUDEPICTURE"):
        t.check_template_file(data)


def test_fields_that_fetch_nothing_are_fine():
    xml = (
        _simple(" PAGE ")
        + f"<w:p>{_complex(' MERGEFIELD ', 'Name ')}</w:p>"
        + _simple(' HYPERLINK "https://www.gov.uk" ')
    )
    t.check_template_file(_with_body_xml(xml))


# The boxes ------------------------------------------------------------------------


def test_the_boxes_are_found_by_tag_once_each_with_their_title_and_options():
    boxes = t.find_boxes(make_template(JUDGMENT, header=["Case Number: ", Sdt("Reference")]))
    by_key = {b.key: b for b in boxes}
    assert [b.key for b in boxes] == [
        "Jurisdiction",
        "Reference",
        "HearingDate",
        "Party1",
        "Party1Title",
        "Party2",
        "Party2Title",
        "Party2Rep",
        "JudgmentText",
        "Year",
    ]
    assert by_key["Party1"].label == "Party1Name"  # the title, matched by its tag
    assert by_key["Reference"].count == 2  # in the body and the header
    assert by_key["Party2Title"].count == 2
    # Word's "Choose an item." is a prompt, not an option.
    assert (by_key["Party1Title"].kind, by_key["Party1Title"].options) == ("dropdown", ["Claimant", "Respondent"])
    assert by_key["HearingDate"].kind == "date"


@pytest.mark.parametrize(
    ("paragraphs", "says"),
    [
        ([["Plain text, no boxes"]], "no fill-in boxes"),
        ([[Sdt(None, placeholder="Party name")]], "no tag or title (it shows “Party name”)"),
        ([[Sdt("Party", kind="dropdown", options=["A"])], [Sdt("Party")]], "both a dropdown and a text"),
        ([[Sdt("Signature", kind="picture")]], "picture or tick box"),
        ([[Sdt("Agreed", kind="checkbox")]], "picture or tick box"),
    ],
    ids=["no-boxes", "untagged", "conflicting-kinds", "picture", "tick-box"],
)
def test_a_box_that_cannot_be_used_is_named(paragraphs, says):
    with pytest.raises(t.TemplateFileError, match=re.escape(says)):
        t.find_boxes(make_template(paragraphs))


def test_each_box_gets_a_field_the_admin_can_change():
    fields = {f.tag: f for f in map(t.propose_field, t.find_boxes(make_template(JUDGMENT)))}
    assert (fields["Reference"].type, fields["Reference"].label, fields["Reference"].required) == (
        "text",
        "Reference",
        True,
    )
    assert (fields["Party1Title"].type, fields["Party1Title"].options) == ("dropdown", ["Claimant", "Respondent"])
    assert fields["HearingDate"].type == "date"
    assert fields["JudgmentText"].type == "dictated"
    assert (fields["Jurisdiction"].type, fields["Jurisdiction"].source) == ("benchnotes", "jurisdiction")
    assert (fields["Year"].type, fields["Year"].source) == ("benchnotes", "year")
    assert not (fields["JudgmentText"].required or fields["Year"].required)  # the judge is not asked


# The fields -----------------------------------------------------------------------


def _proposed():
    boxes = t.find_boxes(make_template(JUDGMENT))
    return boxes, [t.propose_field(b) for b in boxes]


def test_proposed_fields_can_be_saved_as_they_are():
    assert t.field_problems(*_proposed()) == []


def test_a_box_with_no_field_is_named():
    boxes, fields = _proposed()
    problems = t.field_problems(boxes, [f for f in fields if f.tag != "Party2Rep"])
    assert problems == ["The box “Party2Rep” (Party2Rep) has no field linked to it."]


def test_a_field_cannot_be_added():
    boxes, fields = _proposed()
    extra = t.TemplateField(tag="Costs", label="Costs awarded")
    assert t.field_problems(boxes, [*fields, extra]) == [
        "“Costs awarded” (Costs) is not a box in the template: fields cannot be added."
    ]


def test_incomplete_fields_say_what_is_missing():
    boxes, fields = _proposed()
    changed = [
        f.model_copy(update={"type": "dropdown", "options": []})
        if f.tag == "Reference"
        else f.model_copy(update={"source": None})
        if f.tag == "Year"
        else f.model_copy(update={"type": "text"})
        if f.tag == "JudgmentText"
        else f
        for f in fields
    ]
    assert t.field_problems(boxes, changed) == [
        "“Reference” needs at least one option.",
        "Choose what BenchNotes fills “Year” with.",
        "Link one box to the dictated text (the judgment).",
    ]
    two = [f.model_copy(update={"type": "dictated"}) if f.tag == "Party2Rep" else f for f in fields]
    assert t.field_problems(boxes, two) == ["Only one box can take the dictated text: “Party2Rep”, “Judgment text”."]


def test_after_a_new_file_only_the_boxes_it_still_has_keep_their_fields():
    _, fields = _proposed()
    new_boxes = t.find_boxes(make_template([[Sdt("Reference")], [Sdt("Costs")], [Sdt("JudgmentText")]]))
    kept = t.merge_fields(new_boxes, fields)
    assert [f.tag for f in kept] == ["Reference", "JudgmentText"]
    assert t.field_problems(new_boxes, kept) == ["The box “Costs” (Costs) has no field linked to it."]


# The judge's values and the document ------------------------------------------------


def _fields(**changes):
    _, fields = _proposed()
    return [f.model_copy(update=changes[f.tag]) if f.tag in changes else f for f in fields]


def test_the_judges_values_are_checked_against_the_fields():
    fields = _fields(Party2={"required": False}, Party2Title={"required": False}, Party2Rep={"required": False})
    kept, problems = t.check_values(
        fields,
        {"Reference": " 1234/2026 ", "HearingDate": "2026-02-31", "Party1": "", "Party1Title": "Judge", "Extra": "x"},
        [],
    )
    assert problems == [
        "“Extra” is not a field of this template.",
        "“Hearing date” must be a date.",
        "“Party1Name” is required.",
        "“Party1Title” has a choice that is not one of its options.",
    ]
    assert kept == {"Reference": "1234/2026"}


def _filled(fields, values, **context):
    kept, problems = t.check_values(fields, values, list(context.get("legal_issue_titles", {})))
    assert problems == []
    texts = t.box_text(
        fields,
        kept,
        t.FillContext(
            jurisdiction_name="Employment Tribunals (ET)",
            year=2026,
            dictated_text=t.dictated_text(["The claim succeeds."], "<p>Costs are <strong>reserved</strong>.</p>"),
            **context,
        ),
    )
    return texts


def test_the_document_has_the_values_and_unused_optional_lines_are_removed(tmp_path):
    fields = _fields(Party2={"required": False}, Party2Title={"required": False}, Party2Rep={"required": False})
    values = {"Reference": "1234/2026", "HearingDate": "2026-02-12", "Party1": "A Smith", "Party1Title": "Claimant"}
    data = make_template(JUDGMENT, header=["Case Number: ", Sdt("Reference")])
    output = t.fill_template(data, _filled(fields, values), tmp_path / "out.docx")

    document = output.read_bytes()
    assert document_text(document) == [
        "Jurisdiction: Employment Tribunals (ET)",
        "Case Number: 1234/2026",
        "Heard on 12 February 2026",
        "A Smith (Claimant)",
        "The claim succeeds.\n\nCosts are reserved.",
        "© Crown copyright 2026",
    ]
    assert document_text(document, header_part(document)) == ["Case Number: 1234/2026"]


def test_a_paragraph_keeps_its_text_when_only_some_of_its_boxes_are_empty(tmp_path):
    fields = _fields(Party2Title={"required": False}, Party2Rep={"required": False})
    values = {
        "Reference": "1",
        "HearingDate": "2026-02-12",
        "Party1": "A",
        "Party1Title": "Claimant",
        "Party2": "B",
        "Party2Rep": "Ms Counsel",
    }
    output = t.fill_template(make_template(JUDGMENT), _filled(fields, values), tmp_path / "out.docx")
    text = document_text(output.read_bytes())
    assert "B ()" in text
    assert "For the : Ms Counsel" in text


def test_every_field_type_is_written_as_text(tmp_path):
    paragraphs = [[Sdt(tag)] for tag in ("Count", "Agreed", "Reliefs", "Issues", "JudgmentText")]
    data = make_template(paragraphs)
    boxes = t.find_boxes(data)
    fields = [
        t.TemplateField(tag="Count", label="Count", type="number"),
        t.TemplateField(tag="Agreed", label="Agreed", type="yes_no"),
        t.TemplateField(tag="Reliefs", label="Reliefs", type="multi_select", options=["Costs", "Damages", "Interest"]),
        t.TemplateField(tag="Issues", label="Issues", type="legal_issues"),
        t.TemplateField(tag="JudgmentText", label="Judgment", type="dictated"),
    ]
    assert t.field_problems(boxes, fields) == []
    values = {"Count": "3", "Agreed": False, "Reliefs": ["Interest", "Costs"], "Issues": ["unfair", "wages"]}
    titles = {"wages": "Unlawful deductions", "unfair": "Unfair dismissal", "other": "Other"}
    output = t.fill_template(data, _filled(fields, values, legal_issue_titles=titles), tmp_path / "out.docx")
    assert document_text(output.read_bytes())[:4] == [
        "3",
        "No",
        "Interest, Costs",
        "Unlawful deductions, Unfair dismissal",  # in the jurisdiction's order
    ]


def test_choices_must_be_the_fields_options():
    fields = [
        t.TemplateField(tag="Reliefs", label="Reliefs", type="multi_select", options=["Costs"]),
        t.TemplateField(tag="Issues", label="Issues", type="legal_issues"),
        t.TemplateField(tag="Agreed", label="Agreed", type="yes_no"),
        t.TemplateField(tag="Count", label="Count", type="number"),
    ]
    _, problems = t.check_values(
        fields, {"Reliefs": ["Fees"], "Issues": ["x"], "Agreed": "yes", "Count": "three"}, ["y"]
    )
    assert problems == [
        "“Reliefs” has a choice that is not one of its options.",
        "“Issues” has a choice that is not one of its options.",
        "“Agreed” must be yes or no.",
        "“Count” must be a number.",
    ]


def test_the_dictated_text_is_the_transcript_then_the_judges_own_text():
    html = "<h2>Reasons</h2><p>First.<br>Same paragraph.</p><ul><li>One</li><li>Two</li></ul><p></p>"
    assert t.dictated_text([" Spoken. ", ""], html) == "Spoken.\n\nReasons\n\nFirst.\nSame paragraph.\n\n• One\n\n• Two"


def test_a_damaged_part_of_the_file_is_reported_as_damaged():
    """A zip entry whose data does not match its checksum."""
    import io
    import zipfile

    stored = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(make_template(JUDGMENT))) as source, zipfile.ZipFile(stored, "w") as target:
        for item in source.infolist():
            target.writestr(item.filename, source.read(item.filename), compress_type=zipfile.ZIP_STORED)
    data = bytearray(stored.getvalue())
    at = data.index(b"<Relationships")  # the first .rels part, stored as it is
    data[at + 1] ^= 0x20
    with pytest.raises(t.TemplateFileError, match="damaged"):
        t.check_template_file(bytes(data))


def test_a_table_cell_whose_only_box_is_empty_keeps_an_empty_paragraph(tmp_path):
    """Word needs a paragraph in every cell: the cell is emptied, not removed."""
    import io

    from docx import Document
    from docx.oxml import parse_xml

    from tests.unit.template_docx import _sdt_xml

    document = Document()
    document.add_paragraph().add_run("Judgment: ")
    document.paragraphs[-1]._p.append(parse_xml(_sdt_xml(Sdt("JudgmentText"))))
    cell = document.add_table(rows=1, cols=1).cell(0, 0)
    cell.paragraphs[0].add_run("For the claimant: ")
    cell.paragraphs[0]._p.append(parse_xml(_sdt_xml(Sdt("Rep"))))
    out = io.BytesIO()
    document.save(out)
    data = out.getvalue()
    fields = [
        t.TemplateField(tag="JudgmentText", label="Judgment", type="dictated"),
        t.TemplateField(tag="Rep", label="Representative", required=False),
    ]
    assert t.field_problems(t.find_boxes(data), fields) == []

    output = t.fill_template(data, {"JudgmentText": "Granted."}, tmp_path / "out.docx")

    filled = Document(str(output))
    (cell_paragraph,) = filled.tables[0].cell(0, 0).paragraphs
    assert cell_paragraph.text == ""  # neither the label nor the box is left
    assert document_text(output.read_bytes()) == ["Judgment: Granted."]
