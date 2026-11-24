"""DIAAT-344: a Word template uploaded in the admin area, its boxes, the fields linked to
them, and the document a judge's values make from it (documents/uploaded_templates.py)."""

from __future__ import annotations

import io
import re
import zipfile

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


_CELL = "<w:tc><w:p><w:r><w:t>Claimant</w:t></w:r></w:p></w:tc>"


def _table(rows: str) -> str:
    return f'<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid>{rows}</w:tbl>'


def _box(content: str) -> str:
    return f'<w:sdt><w:sdtPr><w:tag w:val="Parties"/></w:sdtPr><w:sdtContent>{content}</w:sdtContent></w:sdt>'


@pytest.mark.parametrize(
    ("xml", "says"),
    [
        (_box(_table(f"<w:tr>{_CELL}</w:tr>")) + "<w:p/>", "holds a table, row or cell"),
        (_table(_box(f"<w:tr>{_CELL}</w:tr>")) + "<w:p/>", "holds a table, row or cell"),
        (_table(f"<w:tr>{_box(_CELL)}</w:tr>") + "<w:p/>", "holds a table, row or cell"),
        (_box("<w:bookmarkStart w:id='0' w:name='b'/><w:bookmarkEnd w:id='0'/>") + "<w:p/>", "has no text"),
    ],
    ids=["table", "row", "cell", "no-text"],
)
def test_a_box_benchnotes_could_not_fill_is_refused_at_upload(xml, says):
    """The value would never reach the document: the placeholder would stay."""
    data = _with_body_xml(xml, make_template([[Sdt("JudgmentText")]]))
    t.check_template_file(data)
    with pytest.raises(t.TemplateFileError, match=f"“Parties” {says}"):
        t.find_boxes(data)


def test_a_box_inside_a_table_cell_is_filled(tmp_path):
    data = _with_body_xml(
        _table(f"<w:tr><w:tc><w:p><w:r><w:t>For: </w:t></w:r>{_box('<w:r><w:t>x</w:t></w:r>')}</w:p></w:tc></w:tr>")
        + "<w:p/>",
        make_template([[Sdt("JudgmentText")]]),
    )
    assert [b.key for b in t.find_boxes(data)] == ["JudgmentText", "Parties"]
    output = t.fill_template(data, {"JudgmentText": "Granted.", "Parties": "Ms A"}, tmp_path / "out.docx")
    assert "For: Ms A" in document_text(output.read_bytes())


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
        "Link at least one box to the dictated text.",
    ]
    # DIAAT-416: several boxes can take dictation, each a section named by its label...
    two = [f.model_copy(update={"type": "dictated"}) if f.tag == "Party2Rep" else f for f in fields]
    assert t.field_problems(boxes, two) == []
    # ...which judges choose it by, so no two can share one.
    same = [f.model_copy(update={"label": " judgment TEXT"}) if f.tag == "Party2Rep" else f for f in two]
    assert t.field_problems(boxes, same) == [
        (
            "Each box for the dictated text is a section judges choose by its name, so each needs its own:"
            " “ judgment TEXT”, “Judgment text”."
        )
    ]


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
            dictated_texts=t.dictated_texts(
                fields, None, ["The claim succeeds."], "<p>Costs are <strong>reserved</strong>.</p>"
            ),
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


# The legal framework text (DIAAT-415) ------------------------------------------------

FRAMEWORK_TEXTS = {
    "protection": "**Protection claims**\n\nThe appellant must show:\n\n1. a real risk\n2. no protection at home",
    "demo_rule": "The Demo Rule applies.\n\n- first point\n- second point",
    "unused": "Never chosen.",
}
FRAMEWORK_TITLES = {"protection": "Protection", "demo_rule": "Demo Rule", "unused": "Unused"}


def _framework_template(**framework):
    data = make_template(
        [["Issues: ", Sdt("Issues")], [Sdt("LegalFramework", "Legal framework")], [Sdt("JudgmentText")]]
    )
    fields = [
        t.TemplateField(tag="Issues", label="Issues", type="legal_issues", required=False),
        t.TemplateField(
            tag="LegalFramework", label="Legal framework", type="benchnotes", source="legal_framework"
        ).model_copy(update=framework),
        t.TemplateField(tag="JudgmentText", label="Judgment", type="dictated"),
    ]
    return data, fields


def _framework_filled(tmp_path, fields, values, data):
    texts = _filled(fields, values, legal_issue_titles=FRAMEWORK_TITLES, legal_framework_texts=FRAMEWORK_TEXTS)
    return t.fill_template(data, texts, tmp_path / "out.docx").read_bytes()


def _paragraphs(document: bytes) -> list[tuple[str, bool]]:
    """Each non-empty paragraph's text, and whether its runs are bold."""
    from lxml import etree

    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(document)).read("word/document.xml"))
    found = []
    for p in root.iter(f"{{{w}}}p"):
        text = "".join(p.xpath(".//w:t/text()", namespaces={"w": w}))
        if text.strip():
            found.append((text, bool(p.xpath(".//w:r/w:rPr/w:b", namespaces={"w": w}))))
    return found


def test_a_box_named_for_the_legal_framework_is_proposed_as_its_text():
    boxes = t.find_boxes(make_template([[Sdt("LegalFramework")], [Sdt("Legal_Framework_Text")]]))
    proposed = [t.propose_field(b) for b in boxes]
    assert [(f.type, f.source, f.required) for f in proposed] == [("benchnotes", "legal_framework", False)] * 2


def test_the_legal_framework_text_needs_a_legal_issues_question_to_follow():
    data, fields = _framework_template()
    boxes = t.find_boxes(data)
    assert t.field_problems(boxes, fields) == []
    without = [f.model_copy(update={"type": "text"}) if f.tag == "Issues" else f for f in fields]
    assert t.field_problems(boxes, without) == [
        (
            "“Legal framework” prints the text of the legal frameworks the judge chooses: link a box"
            " to a Legal issues question for them to choose from."
        )
    ]


def test_the_chosen_frameworks_text_goes_in_its_box_in_order_with_its_formatting(tmp_path):
    data, fields = _framework_template()
    # Chosen in the other order: the text follows the Legal Text Manager's order.
    document = _framework_filled(tmp_path, fields, {"Issues": ["demo_rule", "protection"]}, data)
    assert _paragraphs(document)[:8] == [
        ("Issues: Protection, Demo Rule", False),
        ("Protection claims", True),
        ("The appellant must show:", False),
        ("(a) a real risk", False),
        ("(b) no protection at home", False),
        ("The Demo Rule applies.", False),
        ("• first point", False),
        ("• second point", False),
    ]


def test_one_framework_chosen_gives_only_its_text(tmp_path):
    data, fields = _framework_template()
    document = _framework_filled(tmp_path, fields, {"Issues": ["demo_rule"]}, data)
    assert document_text(document)[:4] == [
        "Issues: Demo Rule",
        "The Demo Rule applies.",
        "• first point",
        "• second point",
    ]


def test_a_framework_chosen_in_two_questions_is_printed_once(tmp_path):
    data = make_template([[Sdt("Issues")], [Sdt("MoreIssues")], [Sdt("LegalFramework")], [Sdt("JudgmentText")]])
    fields = [
        t.TemplateField(tag="Issues", label="Issues", type="legal_issues"),
        t.TemplateField(tag="MoreIssues", label="More issues", type="legal_issues"),
        t.TemplateField(tag="LegalFramework", label="Legal framework", type="benchnotes", source="legal_framework"),
        t.TemplateField(tag="JudgmentText", label="Judgment", type="dictated"),
    ]
    values = {"Issues": ["demo_rule"], "MoreIssues": ["demo_rule"]}
    document = _framework_filled(tmp_path, fields, values, data)
    assert document_text(document).count("The Demo Rule applies.") == 1


def test_with_no_framework_chosen_the_box_and_its_line_are_removed(tmp_path):
    data, fields = _framework_template()
    document = _framework_filled(tmp_path, fields, {}, data)
    assert document_text(document) == ["The claim succeeds.\n\nCosts are reserved."]


def test_with_no_framework_chosen_the_when_left_empty_text_is_printed(tmp_path):
    data, fields = _framework_template(empty_text="No legal framework applies.")
    document = _framework_filled(tmp_path, fields, {}, data)
    assert document_text(document)[0] == "No legal framework applies."


def test_when_left_empty_text_is_kept_only_for_a_legal_framework_box_and_stored_only_when_set():
    _, fields = _framework_template(empty_text="  ")
    fields = [*fields, t.TemplateField(tag="Reference", label="Reference", empty_text="n/a")]
    normalised = {f.tag: f for f in t.normalise_fields(fields)}
    assert normalised["LegalFramework"].empty_text is None
    assert normalised["Reference"].empty_text is None
    assert "empty_text" not in normalised["LegalFramework"].stored()
    set_text = t.normalise_fields(_framework_template(empty_text="None.")[1])[1]
    assert set_text.stored()["empty_text"] == "None."


def test_a_prefill_is_kept_only_for_a_text_field_and_stored_only_when_set():
    fields = [
        t.TemplateField(tag="Judge", label="Judge", prefill="judge_name"),
        t.TemplateField(tag="HearingDate", label="Hearing date", type="date", prefill="judge_name"),
    ]
    judge, day = t.normalise_fields(fields)
    assert judge.stored()["prefill"] == "judge_name"
    assert day.prefill is None
    assert "prefill" not in day.stored()


# Text depending on an answer (DIAAT-411) --------------------------------------------

APPELLANT_ABSENT = (
    "The appellant failed to attend the hearing. I considered **rule 28**.\n\n"
    "It was in the interests of justice to proceed because [REASONS]."
)
RESPONDENT_ABSENT = "The respondent was not represented at the hearing."


def _answer_template(*rules: dict, **extra):
    data = make_template(
        [
            ["Case Number: ", Sdt("Reference")],
            [Sdt("Rule28", "Rule 28")],
            [Sdt("JudgmentText")],
        ]
    )
    fields = [
        t.TemplateField(tag="Reference", label="Case number", required=False),
        t.TemplateField(tag="Attendance", label="Appellant", type="dropdown", options=["Attended", "Did not attend"]),
        t.TemplateField(tag="Represented", label="Respondent represented", type="yes_no"),
        t.TemplateField(tag="Panel", label="Panel", type="multi_select", options=["Lay member", "Doctor"], required=False),
        t.TemplateField(
            tag="Rule28", label="Rule 28", type="benchnotes", source="answer_text", rules=list(rules)
        ).model_copy(update=extra),
        t.TemplateField(tag="JudgmentText", label="Judgment", type="dictated"),
    ]
    return data, fields


RULE_28 = (
    {"field": "Attendance", "answer": "Did not attend", "text": APPELLANT_ABSENT},
    {"field": "Represented", "answer": "no", "text": RESPONDENT_ABSENT},
)


def _answer_filled(tmp_path, fields, values, data) -> bytes:
    return t.fill_template(data, _filled(fields, values), tmp_path / "out.docx").read_bytes()


def test_each_rule_whose_answer_was_given_prints_its_text_in_order(tmp_path):
    data, fields = _answer_template(*RULE_28)
    document = _answer_filled(tmp_path, fields, {"Attendance": "Did not attend", "Represented": False}, data)
    assert document_text(document) == [
        "The appellant failed to attend the hearing. I considered rule 28.",
        "It was in the interests of justice to proceed because [REASONS].",
        "The respondent was not represented at the hearing.",
        "The claim succeeds.\n\nCosts are reserved.",
    ]
    # Formatted as the admin wrote it.
    assert ("rule 28", True, False) in _runs(document, "The appellant failed to attend the hearing. I considered rule 28.")


def test_with_no_rule_applying_the_box_and_its_line_are_removed(tmp_path):
    data, fields = _answer_template(*RULE_28)
    document = _answer_filled(tmp_path, fields, {"Attendance": "Attended", "Represented": True}, data)
    assert document_text(document) == ["The claim succeeds.\n\nCosts are reserved."]


def test_a_rule_can_depend_on_one_choice_of_a_list(tmp_path):
    data, fields = _answer_template({"field": "Panel", "answer": "Doctor", "text": "A doctor sat with me."})
    values = {"Attendance": "Attended", "Represented": True}
    with_doctor = _answer_filled(tmp_path, fields, {**values, "Panel": ["Lay member", "Doctor"]}, data)
    assert document_text(with_doctor)[0] == "A doctor sat with me."
    without = _answer_filled(tmp_path, fields, {**values, "Panel": ["Lay member"]}, data)
    assert document_text(without) == ["The claim succeeds.\n\nCosts are reserved."]


def test_rules_that_cannot_work_are_named():
    _, fields = _answer_template()
    assert t.field_problems(t.find_boxes(_answer_template()[0]), fields)[-1:] == [
        "Add a rule for when BenchNotes fills “Rule 28”."
    ]
    _, fields = _answer_template(
        {"field": "Reference", "answer": "1", "text": "Text."},
        {"field": "Attendance", "answer": "Absent", "text": "Text."},
        {"field": "Represented", "answer": "maybe", "text": "Text."},
        {"field": "Represented", "answer": "yes", "text": "  "},
        {"field": "", "answer": "", "text": "Text."},
        {"field": "Attendance", "answer": "", "text": "Text."},
    )
    problems = t.field_problems([], fields)
    assert [p for p in problems if "of “Rule 28”" in p] == [
        "Rule 1 of “Rule 28” depends on a question that is not a dropdown, list or yes/no question.",
        "Rule 2 of “Rule 28” depends on an answer “Appellant” no longer has: choose another.",
        "Rule 3 of “Rule 28” depends on an answer “Respondent represented” no longer has: choose another.",
        "Rule 4 of “Rule 28” has no text to print.",
        "Choose the question rule 5 of “Rule 28” depends on.",
        "Choose the answer of “Appellant” rule 6 of “Rule 28” depends on.",
    ]


def test_rules_are_kept_only_for_their_box_and_stored_only_when_set():
    _, fields = _answer_template(*RULE_28)
    fields = [*fields, t.TemplateField(tag="Other", label="Other", rules=[t.AnswerRule(**RULE_28[0])])]
    normalised = {f.tag: f for f in t.normalise_fields(fields)}
    assert [r["answer"] for r in normalised["Rule28"].stored()["rules"]] == ["Did not attend", "no"]
    assert normalised["Other"].rules is None
    assert "rules" not in normalised["Other"].stored()
    assert "rules" not in normalised["Reference"].stored()


# Answers in a rule's text (DIAAT-412) ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "answers", "printed"),
    [
        # The IAC's representative sentences.
        ("{{Name}}, Home Office Presenting Officer", {"Name": "J Bloggs"}, ["J Bloggs, Home Office Presenting Officer"]),
        ("{{Name}}, Counsel", {"Name": ""}, ["Counsel"]),
        ("Counsel, {{Name}}", {"Name": ""}, ["Counsel"]),
        ("X, {{Name}}, Y", {"Name": ""}, ["X, Y"]),
        ("{{First}}, {{Second}}, Counsel", {"First": "", "Second": "B"}, ["B, Counsel"]),
        # Empty: no gap, stray space, comma, formatting or brackets left behind.
        ("Heard by {{Name}} today.", {"Name": ""}, ["Heard by today."]),
        ("Heard by {{Name}}.", {"Name": ""}, ["Heard by."]),
        ("{{Name}} attended.", {"Name": ""}, ["attended."]),
        ("**{{Name}}**, Counsel", {"Name": ""}, ["Counsel"]),
        ("Counsel (*{{Name}}*) attended", {"Name": ""}, ["Counsel attended"]),
        ("Counsel ({{Name}}, of chambers)", {"Name": ""}, ["Counsel (of chambers)"]),
        ("Counsel [{{Name}}] attended", {"Name": ""}, ["Counsel attended"]),
        ("Counsel **({{Name}})** attended", {"Name": ""}, ["Counsel attended"]),
        ("Counsel <u>{{Name}}</u>, attended", {"Name": ""}, ["Counsel attended"]),
        ("Counsel ({{First}} {{Last}}) attended", {"First": "", "Last": ""}, ["Counsel attended"]),
        ("**{{Name}},** Counsel", {"Name": ""}, ["Counsel"]),
        # Only what wraps nothing but empty answers goes.
        ("**Counsel {{Name}}**", {"Name": ""}, ["Counsel"]),
        ("Counsel ({{First}} {{Last}})", {"First": "", "Last": "Bloggs"}, ["Counsel (Bloggs)"]),
        # A list item left with nothing goes; the admin's own lines stay.
        ("Present:\n\n- {{A}}\n- {{B}}", {"A": "", "B": "X"}, ["Present:", "• X"]),
        # As typed, whatever it holds: nothing in an answer formats the document.
        ("**For:** {{Name}}", {"Name": "*J* [x](y) # 1. a_b\n\nc"}, ["For: *J* [x](y) # 1. a_b c"]),
        ("{{Name}}", {"Name": "<u>x</u> \\*y\\*"}, ["<u>x</u> \\*y\\*"]),
        ("Ref `{{Ref}}` and ``{{Ref}}`` and \\`{{Ref}}", {"Ref": "x` **b** `y"}, ["Ref x` **b** `y and x` **b** `y and `x` **b** `y"]),
        ("```\n{{Ref}}\n```", {"Ref": "A-1"}, ["A-1"]),
        # Single braces, which text saved before placeholders may hold, are never read as one;
        # nor is a placeholder naming no question.
        ("Keep {Name} and {{Other}}", {"Name": "x"}, ["Keep {Name} and {{Other}}"]),
    ],
)
def test_answers_are_filled_in_as_typed_with_no_gap_where_one_is_empty(text, answers, printed):
    assert [p.shown_text for p in t.answer_paragraphs(text, answers)] == printed


def test_an_answer_takes_the_formatting_of_where_it_is():
    (paragraph,) = t.answer_paragraphs("For **{{Name}}**, Counsel", {"Name": "*J*"})
    assert [(r.text, r.bold) for r in paragraph.runs] == [("For ", False), ("*J*", True), (", Counsel", False)]
    assert not paragraph.underline


def test_a_rule_prints_the_answers_as_their_boxes_would(tmp_path):
    data, fields = _answer_template(
        {"field": "Represented", "answer": "yes", "text": "{{Reference}}, for the respondent, on {{Heard}}: {{Panel}}."},
        {"field": "Represented", "answer": "no", "text": "{{Reference}}, Counsel"},
    )
    fields.insert(1, t.TemplateField(tag="Heard", label="Heard", type="date", required=False))
    values = {"Attendance": "Attended", "Reference": "J *Bloggs*", "Heard": "2026-02-12", "Panel": ["Doctor"]}
    present = _answer_filled(tmp_path, fields, {**values, "Represented": True}, data)
    assert document_text(present)[1] == "J *Bloggs*, for the respondent, on 12 February 2026: Doctor."
    # Underlining typed by the judge is printed, not applied.
    underlined = _answer_filled(tmp_path, fields, {**values, "Reference": "<u>J</u>", "Represented": True}, data)
    assert document_text(underlined)[1].startswith("<u>J</u>, for the respondent")
    assert _runs(present, "J *Bloggs*, for the respondent, on 12 February 2026: Doctor.")[0][1:] == (False, False)
    unnamed = _answer_filled(tmp_path, fields, {"Attendance": "Attended", "Represented": False}, data)
    assert document_text(unnamed)[0] == "Counsel"


def test_a_rule_printing_an_answer_that_is_not_a_question_is_named():
    _, fields = _answer_template(
        {"field": "Represented", "answer": "no", "text": "{{Rule28}} {{JudgmentText}} {{Nobody}} {{Nobody}} {{Reference}} {sic}"},
    )
    assert [p for p in t.field_problems([], fields) if "prints the answer" in p] == [
        f"Rule 1 of “Rule 28” prints the answer to “{tag}”, which is not a question the judge answers."
        for tag in ("Rule28", "JudgmentText", "Nobody")
    ]


def _runs(document: bytes, text: str) -> list[tuple[str, bool, bool]]:
    """The runs of the paragraph reading ``text``: their text, bold and italic."""
    from lxml import etree

    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(document)).read("word/document.xml"))
    for p in root.iter(f"{{{w}}}p"):
        if "".join(p.xpath(".//w:t/text()", namespaces={"w": w})) == text:
            return [
                (
                    "".join(r.xpath("./w:t/text()", namespaces={"w": w})),
                    bool(r.xpath("./w:rPr/w:b", namespaces={"w": w})),
                    bool(r.xpath("./w:rPr/w:i", namespaces={"w": w})),
                )
                for r in p.xpath(".//w:r[w:t]", namespaces={"w": w})
            ]
    raise AssertionError(f"no paragraph reads {text!r}")


def test_bold_and_italic_within_a_line_stay_on_their_words(tmp_path):
    data, fields = _framework_template()
    texts = _filled(
        fields,
        {"Issues": ["protection"]},
        legal_issue_titles=FRAMEWORK_TITLES,
        legal_framework_texts={
            "protection": "Section 82 of the **2002 Act**, as in *Smith v Jones*.\n\n1. a **real** risk"
        },
    )
    document = t.fill_template(data, texts, tmp_path / "out.docx").read_bytes()
    assert _runs(document, "Section 82 of the 2002 Act, as in Smith v Jones.") == [
        ("Section 82 of the ", False, False),
        ("2002 Act", True, False),
        (", as in ", False, False),
        ("Smith v Jones", False, True),
        (".", False, False),
    ]
    assert _runs(document, "(a) a real risk") == [
        ("(a) ", False, False),
        ("a ", False, False),
        ("real", True, False),
        (" risk", False, False),
    ]


def _box_lines(markdown: str) -> list[tuple[str | None, float | None]]:
    from transcribe_api.documents.lib.markdown_to_blocks import markdown_to_blocks

    return [(p.label, p.left_indent_pt) for p in t.box_paragraphs(markdown_to_blocks(markdown))]


def test_list_labels_go_on_past_z_and_x_rather_than_starting_again():
    labels = [label for label, _ in _box_lines("\n".join(f"{i}. item" for i in range(1, 29)))]
    assert labels[25:] == ["(z)", "(aa)", "(ab)"]
    nested = _box_lines("1. top\n" + "\n".join(f"   {i}. item" for i in range(1, 12)))
    assert [label for label, _ in nested[-2:]] == ["(x)", "(xi)"]


def test_a_nested_bulleted_list_stays_bulleted_and_indented_further():
    assert _box_lines("- First\n  - Nested\n- Second") == [("•", 36.0), ("•", 72.0), ("•", 36.0)]
    assert _box_lines("1. one\n   - point\n2. two") == [("(a)", 36.0), ("•", 72.0), ("(b)", 36.0)]


def test_in_a_quote_bold_stays_on_its_words_and_the_quote_is_italic_throughout():
    from transcribe_api.documents.lib.markdown_to_blocks import markdown_to_blocks

    item, heading = t.box_paragraphs(markdown_to_blocks("> 1. a **real** risk\n\n> **Scope**"))
    assert [(r.text, r.bold, r.italic) for r in item.runs] == [
        ("(a)\t", False, True),
        ("a ", False, True),
        ("real", True, True),
        (" risk", False, True),
    ]
    assert [(r.text, r.bold, r.italic) for r in heading.runs] == [("Scope", True, True)]


def _framework_box_xml(data: bytes, change) -> bytes:
    """``data`` with the paragraph holding the LegalFramework box changed by ``change(p)``."""
    from lxml import etree

    def transform(content: bytes) -> bytes:
        root = etree.fromstring(content)
        w = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        (paragraph,) = root.xpath("//w:p[w:sdt/w:sdtPr/w:tag[@w:val='LegalFramework']]", namespaces=w)
        change(paragraph, w)
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    return replace_part(data, "word/document.xml", transform)


def test_bold_is_written_even_where_the_template_turns_it_off(tmp_path):
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls
    from lxml import etree

    def plain(paragraph, w):
        (run,) = paragraph.xpath("w:sdt/w:sdtContent/w:r", namespaces=w)
        run.insert(0, parse_xml(f'<w:rPr {nsdecls("w")}><w:b w:val="0"/><w:u w:val="none"/></w:rPr>'))

    data, fields = _framework_template()
    document = _framework_filled(tmp_path, fields, {"Issues": ["protection"]}, _framework_box_xml(data, plain))
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(document)).read("word/document.xml"))
    w = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    def formatting(text):
        (run,) = root.xpath(f"//w:r[w:t='{text}']", namespaces=w)
        return [
            (e.tag.split("}")[1], e.get(f"{{{w['w']}}}val")) for e in run.xpath("w:rPr/w:b | w:rPr/w:u", namespaces=w)
        ]

    assert formatting("Protection claims") == [("b", None), ("u", "none")]
    assert formatting("The appellant must show:") == [("b", "0"), ("u", "none")]


def test_a_bookmark_around_the_framework_box_is_kept(tmp_path):
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    def bookmarked(paragraph, w):
        (sdt,) = paragraph.xpath("w:sdt", namespaces=w)
        sdt.addprevious(parse_xml(f'<w:bookmarkStart {nsdecls("w")} w:id="7" w:name="Framework"/>'))
        sdt.addnext(parse_xml(f'<w:bookmarkEnd {nsdecls("w")} w:id="7"/>'))

    data, fields = _framework_template()
    document = _framework_filled(tmp_path, fields, {"Issues": ["demo_rule"]}, _framework_box_xml(data, bookmarked))
    xml = zipfile.ZipFile(io.BytesIO(document)).read("word/document.xml").decode()
    start, box, end = xml.index('w:name="Framework"'), xml.index("The Demo Rule applies."), xml.index("w:bookmarkEnd")
    assert start < box < end
    assert document_text(document)[1:4] == ["The Demo Rule applies.", "• first point", "• second point"]


def test_underline_and_italic_are_written_even_where_the_template_turns_them_off():
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls, qn

    from transcribe_api.documents.word_document import WordDocument

    template = parse_xml(f'<w:rPr {nsdecls("w")}><w:i w:val="false"/><w:u w:val="none"/></w:rPr>')
    rpr = WordDocument._formatted_rpr(template, bold=False, italic=True, underline=True)
    assert rpr.find(qn("w:i")).get(qn("w:val")) is None
    assert rpr.find(qn("w:u")).get(qn("w:val")) == "single"
    assert rpr.find(qn("w:b")) is None


def test_a_list_items_second_paragraph_is_not_a_new_item():
    assert _box_lines("1. first\n\n   more of it\n2. second") == [("(a)", 36.0), (None, 36.0), ("(b)", 36.0)]


def test_italic_within_a_heading_stays_on_its_words():
    from transcribe_api.documents.lib.markdown_to_blocks import markdown_to_blocks

    (heading,) = t.box_paragraphs(markdown_to_blocks("## Scope of *Smith v Jones*"))
    assert (heading.bold, heading.underline) == (True, True)
    assert [(r.text, r.bold, r.italic) for r in heading.runs] == [
        ("Scope of ", False, False),
        ("Smith v Jones", False, True),
    ]


def test_list_lines_are_not_numbered_again_by_the_boxs_list_style(tmp_path):
    from lxml import etree

    data, fields = _framework_template()
    document = _framework_filled(tmp_path, fields, {"Issues": ["demo_rule"]}, data)
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(document)).read("word/document.xml"))
    w = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    (item,) = [
        p
        for p in root.xpath("//w:p", namespaces=w)
        if "".join(p.xpath(".//w:t/text()", namespaces=w)) == "• first point"
    ]
    assert item.xpath("w:pPr/w:numPr/w:numId/@w:val", namespaces=w) == ["0"]


def test_a_bookmark_inside_a_box_around_a_paragraph_is_kept(tmp_path):
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    def box_around_paragraph(paragraph, w):
        # <w:p><w:sdt>…<w:r/>…</w:sdt></w:p> → <w:sdt>…<w:p>bookmark <w:r/> bookmark</w:p>…</w:sdt>
        (sdt,) = paragraph.xpath("w:sdt", namespaces=w)
        (content,) = sdt.xpath("w:sdtContent", namespaces=w)
        inner = parse_xml(f"<w:p {nsdecls('w')}/>")
        inner.append(parse_xml(f'<w:bookmarkStart {nsdecls("w")} w:id="7" w:name="Framework"/>'))
        for run in list(content):
            inner.append(run)
        inner.append(parse_xml(f'<w:bookmarkEnd {nsdecls("w")} w:id="7"/>'))
        content.append(inner)
        paragraph.addprevious(sdt)
        paragraph.getparent().remove(paragraph)

    data, fields = _framework_template()
    template = _framework_box_xml(data, box_around_paragraph)
    document = _framework_filled(tmp_path, fields, {"Issues": ["demo_rule"]}, template)
    xml = zipfile.ZipFile(io.BytesIO(document)).read("word/document.xml").decode()
    start, first, last, end = (
        xml.index('w:name="Framework"'),
        xml.index("The Demo Rule applies."),
        xml.index("second point"),
        xml.index("w:bookmarkEnd"),
    )
    assert start < first < last < end


# Dictation sections (DIAAT-416) --------------------------------------------------------

SECTION_FIELDS = [
    t.TemplateField(tag="Reference", label="Case number", type="text"),
    t.TemplateField(tag="Background", label="Background", type="dictated"),
    t.TemplateField(tag="Findings", label="Findings of fact", type="dictated"),
    t.TemplateField(tag="Decision", label="Decision", type="dictated"),
]


def test_the_templates_dictated_boxes_are_its_sections_in_order():
    assert [f.tag for f in t.dictation_sections(SECTION_FIELDS)] == ["Background", "Findings", "Decision"]
    assert t.unknown_sections(SECTION_FIELDS, {"Background": [], "Evidence": []}) == ["Evidence"]


def test_each_section_gets_only_its_own_dictation_and_the_notes_follow_the_last():
    texts = t.dictated_texts(
        SECTION_FIELDS,
        {"Background": ["Heard today."], "Decision": [" The claim succeeds. "]},
        ["(not used: the sections are)"],
        "<p>Costs are reserved.</p>",
    )
    assert texts == {
        "Background": "Heard today.",
        "Findings": "",  # nothing dictated: its box is removed
        "Decision": "The claim succeeds.\n\nCosts are reserved.",
    }


def test_without_sections_all_of_the_dictation_goes_in_the_first_box():
    one_box = [f for f in SECTION_FIELDS if f.tag in ("Reference", "Decision")]
    # One box, as before sections: the dictation and then the notes.
    assert t.dictated_texts(one_box, None, ["Spoken."], "<p>Typed.</p>") == {"Decision": "Spoken.\n\nTyped."}
    # A page from before sections, with a template that has several: all in the first,
    # the notes too, rather than split from the dictation they follow.
    assert t.dictated_texts(SECTION_FIELDS, None, ["Spoken."], "<p>Typed.</p>") == {
        "Background": "Spoken.\n\nTyped.",
        "Findings": "",
        "Decision": "",
    }


def test_the_sections_must_hold_what_was_dictated_in_the_templates_order():
    sections = {"Decision": ["Second."], "Background": ["First."]}
    assert t.sections_cover(SECTION_FIELDS, sections, ["First.", "Second."])
    assert not t.sections_cover(SECTION_FIELDS, sections, ["First.", "Second.", "Left out."])
    assert not t.sections_cover(SECTION_FIELDS, {}, ["Spoken."])


def test_the_preview_names_each_sections_box():
    from transcribe_api.jurisdiction_templates import preview_dictated_texts

    texts = preview_dictated_texts(SECTION_FIELDS)
    assert list(texts) == ["Background", "Findings", "Decision"]
    assert texts["Findings"].startswith("This is where the judge's dictation for “Findings of fact” goes.")
