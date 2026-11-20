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
        "Link at least one box to the dictated text (the judgment).",
    ]
    # Several dictated boxes are sections, told apart by their names.
    two = [f.model_copy(update={"type": "dictated"}) if f.tag == "Party2Rep" else f for f in fields]
    assert t.field_problems(boxes, two) == []
    same_name = [f.model_copy(update={"label": "Judgment text"}) if f.tag == "Party2Rep" else f for f in two]
    assert t.field_problems(boxes, same_name) == [
        ("Two dictated boxes are both called “Judgment text”: judges pick a section by its name, so give each its own.")
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
        {},
    )
    assert problems == [
        "“Extra” is not a field of this template.",
        "“Hearing date” must be a date.",
        "“Party1Name” is required.",
        "“Party1Title” has a choice that is not one of its options.",
    ]
    assert kept == {"Reference": "1234/2026"}


def _filled(fields, values, content=None, transcripts=None, notes="<p>Costs are <strong>reserved</strong>.</p>"):
    content = content or {}
    choices = {kind: list(options) for kind, options in t.content_choices(content).items()}
    kept, problems = t.check_values(fields, values, choices)
    assert problems == []
    first = next(f.tag for f in fields if f.type == "dictated")
    transcripts = {first: ["The claim succeeds."]} if transcripts is None else transcripts
    return t.box_text(
        fields,
        kept,
        t.FillContext(
            jurisdiction_name="Employment Tribunals (ET)",
            year=2026,
            sections=t.dictated_sections(fields, transcripts, notes),
            content=content,
        ),
    )


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
        "The claim succeeds.",
        "Your notes",
        "Costs are reserved.",
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
    output = t.fill_template(data, _filled(fields, values, content=CONTENT), tmp_path / "out.docx")
    # Legal issues as the built-in decision listed them: each chosen framework's
    # questions, (a), (b)..., in the Legal Text Manager's order.
    assert document_text(output.read_bytes())[:6] == [
        "3",
        "No",
        "Interest, Costs",
        "(a) Were wages deducted?",
        "(b) Was the dismissal fair?",
        "(c) Was there a fair procedure?",
    ]


def test_choices_must_be_the_fields_options():
    fields = [
        t.TemplateField(tag="Reliefs", label="Reliefs", type="multi_select", options=["Costs"]),
        t.TemplateField(tag="Issues", label="Issues", type="legal_issues"),
        t.TemplateField(tag="Agreed", label="Agreed", type="yes_no"),
        t.TemplateField(tag="Count", label="Count", type="number"),
    ]
    _, problems = t.check_values(
        fields, {"Reliefs": ["Fees"], "Issues": ["x"], "Agreed": "yes", "Count": "three"}, {"legal_issues": ["y"]}
    )
    assert problems == [
        "“Reliefs” has a choice that is not one of its options.",
        "“Issues” has a choice that is not one of its options.",
        "“Agreed” must be yes or no.",
        "“Count” must be a number.",
    ]


def _location_fields(**other):
    return [
        t.TemplateField(tag="Location", label="Hearing location", type="dropdown", options=["Taylor House", "Other"]),
        t.TemplateField(
            tag="LocationOther",
            label="Specify location",
            show_when=t.ShowWhen(tag="Location", answers=["Other"]),
            **other,
        ),
        t.TemplateField(tag="JudgmentText", label="Judgment", type="dictated"),
    ]


def test_a_question_is_asked_only_after_the_answers_chosen_for_it():
    fields = _location_fields()
    # Not asked: not required, and an answer sent anyway is dropped.
    kept, problems = t.check_values(fields, {"Location": "Taylor House", "LocationOther": "Leeds"}, {})
    assert (kept, problems) == ({"Location": "Taylor House"}, [])
    # Asked: as any other question.
    kept, problems = t.check_values(fields, {"Location": "Other"}, {})
    assert problems == ["“Specify location” is required."]
    kept, problems = t.check_values(fields, {"Location": "Other", "LocationOther": "Leeds"}, {})
    assert (kept, problems) == ({"Location": "Other", "LocationOther": "Leeds"}, [])


def test_a_question_asked_after_one_not_asked_is_not_asked_either():
    fields = [
        t.TemplateField(tag="Attended", label="Attended", type="yes_no"),
        t.TemplateField(
            tag="Rep",
            label="Representation",
            type="dropdown",
            options=["Represented", "Themselves"],
            show_when=t.ShowWhen(tag="Attended", answers=["yes"]),
        ),
        t.TemplateField(
            tag="RepName", label="Representative", show_when=t.ShowWhen(tag="Rep", answers=["Represented"])
        ),
    ]
    assert t.hidden_fields(fields, {"Attended": False, "Rep": "Represented"}) == {"Rep", "RepName"}
    assert t.hidden_fields(fields, {"Attended": True, "Rep": "Represented"}) == set()
    assert t.hidden_fields(fields, {"Attended": True, "Rep": "Themselves"}) == {"RepName"}


def test_a_show_when_must_name_a_question_with_set_answers():
    data = make_template([[Sdt("Location")], [Sdt("LocationOther")], [Sdt("Notes")], [Sdt("JudgmentText")]])
    boxes = t.find_boxes(data)
    fields = [*_location_fields(), t.TemplateField(tag="Notes", label="Notes")]
    assert t.field_problems(boxes, fields) == []

    def changed(rule):
        return [f.model_copy(update={"show_when": rule}) if f.tag == "LocationOther" else f for f in fields]

    assert t.field_problems(boxes, changed(t.ShowWhen(tag="Notes", answers=["x"]))) == [
        "“Specify location” can only be asked after a question with set answers: choose another."
    ]
    assert t.field_problems(boxes, changed(t.ShowWhen(tag="Location", answers=[]))) == [
        "Choose which answers to “Hearing location” ask “Specify location”."
    ]
    assert t.field_problems(boxes, changed(t.ShowWhen(tag="Location", answers=["Leeds"]))) == [
        "“Specify location” is asked after an answer “Hearing location” no longer has: choose again."
    ]
    looped = [
        f.model_copy(
            update={"type": "dropdown", "options": ["a"], "show_when": t.ShowWhen(tag="Location", answers=["a"])}
        )
        if f.tag == "Notes"
        else f.model_copy(update={"show_when": t.ShowWhen(tag="Notes", answers=["a"])})
        if f.tag == "Location"
        else f
        for f in fields
    ]
    assert "“Hearing location” waits on a question that waits on it: one of them must be asked first." in (
        t.field_problems(boxes, looped)
    )


def test_a_box_left_empty_or_not_asked_prints_its_empty_text(tmp_path):
    fields = [
        *_location_fields(empty_text="Not applicable"),
        t.TemplateField(tag="Fee", label="Fee award", required=False, empty_text="No fee award is made."),
    ]
    data = make_template([[Sdt("Location")], [Sdt("LocationOther")], [Sdt("Fee")], [Sdt("JudgmentText")]])
    output = t.fill_template(data, _filled(fields, {"Location": "Taylor House"}, notes=""), tmp_path / "out.docx")
    assert document_text(output.read_bytes()) == [
        "Taylor House",
        "Not applicable",
        "No fee award is made.",
        "The claim succeeds.",
    ]


def test_settings_that_do_not_fit_the_type_are_dropped():
    rule = t.ShowWhen(tag="Location", answers=["Other"])
    fields = t.normalise_fields(
        [
            t.TemplateField(tag="Name", label="Judge", prefill="judge_name", empty_text=" ", show_when=rule),
            t.TemplateField(tag="Day", label="Day", type="date", prefill="judge_name", empty_text="Not known"),
            t.TemplateField(tag="Year", label="Year", type="benchnotes", source="year", empty_text="x", show_when=rule),
        ]
    )
    assert [(f.prefill, f.empty_text, f.show_when) for f in fields] == [
        ("judge_name", None, rule),
        (None, "Not known", None),
        (None, None, None),
    ]


def test_each_dictated_section_gets_its_dictation_then_the_notes_under_its_heading():
    fields = [
        t.TemplateField(tag="Background", label="Background", type="dictated"),
        t.TemplateField(tag="Evidence", label="Evidence", type="dictated"),
        t.TemplateField(tag="Facts", label="Findings of fact", type="dictated"),
    ]
    notes = (
        "<p>Before any heading.</p><h3>Evidence</h3><ul><li><p><strong>Bold</strong> point</p></li></ul>"
        "<h3>Other</h3><blockquote><p>Quoted.</p></blockquote><h3>Facts</h3><ol><li><p>One</p></li></ol>"
    )
    sections = t.dictated_sections(
        fields, {"Background": [" Spoken. ", ""], "Evidence": ["Heard."], "background": ["From an older page."]}, notes
    )
    texts = t.box_text(fields, {}, t.FillContext(jurisdiction_name="ET", year=2026, sections=sections))
    assert [p.shown_text for p in texts["Background"]] == [
        "Spoken.",
        "From an older page.",  # a section the template does not have: its first
        "Your notes",
        "Before any heading.",
    ]
    evidence = texts["Evidence"]
    assert [p.shown_text for p in evidence] == ["Heard.", "Your notes", "(a) Bold point", "Other", "Quoted."]
    assert evidence[2].bold and evidence[3].bold and evidence[4].italic
    # "Facts" is the IAC's name for findings of fact: its notes still go there.
    assert [p.shown_text for p in texts["Facts"]] == ["(a) One"]
    empty = t.dictated_sections(fields, {}, None)
    texts = t.box_text(fields, {}, t.FillContext(jurisdiction_name="ET", year=2026, sections=empty))
    assert [p.shown_text for p in texts["Facts"]] == ["[No findings of fact content]"]


# Document Content: legal issues, hearing type and anonymity ----------------------------

CONTENT = {
    "legal_frameworks": [
        {
            "id": "unfair",
            "title": "Unfair dismissal",
            "rank": 2,
            "content": "## Unfair dismissal\n\nSection 98 **applies**.\n\n1. Reason\n2. Fairness",
            "issues": ["Was the dismissal fair?", "Was there a fair procedure?"],
        },
        {
            "id": "wages",
            "title": "Unlawful deductions",
            "rank": 1,
            "content": "Part II.",
            "issues": ["Were wages deducted?"],
        },
        {"id": "other", "title": "Other", "rank": 3, "content": "Other law.", "issues": []},
    ],
    "hearing_types": [
        {"id": "cvp", "title": "Video", "hearing_title": "The Hearing", "hearing_description": "Heard by **video**."},
        {
            "id": "papers",
            "title": "On the papers",
            "hearing_title": "Without a hearing",
            "hearing_description": "None.",
        },
    ],
    "anonymity": {
        "statuses": [
            {
                "id": "granted",
                "identifier": "Yes",
                "header": "(ANONYMITY ORDER MADE)",
                "content": "I have made an order.",
                "order_header": "ANONYMITY ORDER",
                "order_text": "No one shall identify the appellant.",
            },
            {
                "id": "not_sought",
                "identifier": "No",
                "header": "[DELETE]",
                "content": "[DELETE]",
                "order_header": "[DELETE]",
                "order_text": "[DELETE]",
            },
        ]
    },
}

CONTENT_FIELDS = [
    t.TemplateField(tag="Issues", label="Legal issues", type="legal_issues"),
    t.TemplateField(tag="Framework", label="Legal framework", type="benchnotes", source="legal_framework"),
    t.TemplateField(tag="Hearing", label="Hearing type", type="hearing_type"),
    t.TemplateField(tag="HearingText", label="Hearing", type="benchnotes", source="hearing_description"),
    t.TemplateField(tag="Anonymity", label="Anonymity", type="anonymity"),
    t.TemplateField(tag="Reasons", label="Reasons", type="benchnotes", source="anonymity_reasons"),
    t.TemplateField(tag="OrderHeading", label="Order heading", type="benchnotes", source="anonymity_order_heading"),
    t.TemplateField(tag="Order", label="Order", type="benchnotes", source="anonymity_order"),
    t.TemplateField(tag="Background", label="Background", type="dictated"),
]


def test_the_document_content_options_are_what_the_judge_chooses_from():
    assert t.content_choices(CONTENT) == {
        "legal_issues": {"wages": "Unlawful deductions", "unfair": "Unfair dismissal", "other": "Other"},
        "hearing_type": {"cvp": "Video", "papers": "On the papers"},
        "anonymity": {"granted": "Yes", "not_sought": "No"},
    }
    choices = {kind: list(options) for kind, options in t.content_choices(CONTENT).items()}
    _, problems = t.check_values(CONTENT_FIELDS, {"Issues": ["gone"], "Hearing": "x", "Anonymity": "maybe"}, choices)
    assert problems == [
        "“Legal issues” has a choice that is not one of its options.",
        "“Hearing type” has a choice that is not one of its options.",
        "“Anonymity” has a choice that is not one of its options.",
    ]


def test_boxes_are_filled_from_the_document_content_as_the_built_in_decision_was(tmp_path):
    data = make_template([[Sdt(f.tag)] for f in CONTENT_FIELDS])
    values = {"Issues": ["unfair"], "Hearing": "cvp", "Anonymity": "granted"}
    output = t.fill_template(data, _filled(CONTENT_FIELDS, values, content=CONTENT), tmp_path / "out.docx")
    assert document_text(output.read_bytes()) == [
        "(a) Was the dismissal fair?",
        "(b) Was there a fair procedure?",
        "Unfair dismissal",
        "Section 98 applies.",
        "(a) Reason",
        "(b) Fairness",
        "The Hearing",
        "Heard by video.",
        "(ANONYMITY ORDER MADE)",
        "I have made an order.",
        "ANONYMITY ORDER",
        "No one shall identify the appellant.",
        "The claim succeeds.",
        "Your notes",
        "Costs are reserved.",
    ]


def test_wording_the_answer_leaves_out_takes_its_line_with_it(tmp_path):
    data = make_template([["Before"], *[[Sdt(f.tag)] for f in CONTENT_FIELDS], ["After"]])
    values = {"Issues": ["other"], "Hearing": "papers", "Anonymity": "not_sought"}
    output = t.fill_template(data, _filled(CONTENT_FIELDS, values, content=CONTENT), tmp_path / "out.docx")
    assert document_text(output.read_bytes()) == [
        "Before",
        # "other" has no questions: no list.
        "Other law.",
        "Without a hearing",
        "None.",
        "The claim succeeds.",
        "Your notes",
        "Costs are reserved.",
        "After",
    ]


def test_wording_follows_an_answer_the_template_must_ask_for():
    boxes = [t.Box(key=f.tag, label=f.label, kind="text") for f in CONTENT_FIELDS]
    assert t.field_problems(boxes, CONTENT_FIELDS) == []
    without = [f for f in CONTENT_FIELDS if f.type not in t.CONTENT_TYPES]
    assert t.field_problems(boxes, [*without, *CONTENT_FIELDS[:1]]) == [
        "The box “Hearing type” (Hearing) has no field linked to it.",
        "The box “Anonymity” (Anonymity) has no field linked to it.",
        "“Hearing” follows the judge's hearing type answer: link a box to Hearing type too.",
        "“Reasons” follows the judge's anonymity answer: link a box to Anonymity too.",
        "“Order heading” follows the judge's anonymity answer: link a box to Anonymity too.",
        "“Order” follows the judge's anonymity answer: link a box to Anonymity too.",
    ]
    twice = [*CONTENT_FIELDS, t.TemplateField(tag="Issues2", label="More issues", type="legal_issues")]
    boxes2 = [*boxes, t.Box(key="Issues2", label="More issues", kind="text")]
    assert t.field_problems(boxes2, twice) == ["Only one box can be Legal issues: “Legal issues”, “More issues”."]


def test_a_box_inside_a_line_keeps_its_line_and_the_formatting(tmp_path):
    data = make_template([["Framework: ", Sdt("Framework"), "."], [Sdt("Issues")], [Sdt("Background")]])
    fields = [CONTENT_FIELDS[0], CONTENT_FIELDS[1], CONTENT_FIELDS[-1]]
    output = t.fill_template(data, _filled(fields, {"Issues": ["wages"]}, content=CONTENT), tmp_path / "out.docx")
    text = document_text(output.read_bytes())
    assert text[0] == "Framework: Part II.."
    assert text[1] == "(a) Were wages deducted?"


def test_the_proposed_types_know_the_iac_template_s_boxes():
    proposed = {
        tag: (f.type, f.source)
        for tag in (
            "LegalIssues",
            "LegalFrameworkContent",
            "HearingType",
            "HearingDescription",
            "AnonymityHead",
            "AnonymityReasonsText",
            "AnonymityOrderText",
            "Background",
            "Evidence",
            "Facts",
        )
        for f in [t.propose_field(t.Box(key=tag, label=tag, kind="text"))]
    }
    assert proposed == {
        "LegalIssues": ("legal_issues", None),
        "LegalFrameworkContent": ("benchnotes", "legal_framework"),
        "HearingType": ("hearing_type", None),
        "HearingDescription": ("benchnotes", "hearing_description"),
        "AnonymityHead": ("anonymity", None),
        "AnonymityReasonsText": ("benchnotes", "anonymity_reasons"),
        "AnonymityOrderText": ("benchnotes", "anonymity_order"),
        "Background": ("dictated", None),
        "Evidence": ("dictated", None),
        "Facts": ("dictated", None),
    }


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
