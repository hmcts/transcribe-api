"""DIAAT-344: the product team uploads a jurisdiction's Word template in the admin area,
links each box to a field, and judges choose it and get a document from it, with no
code change or release. Real lookups on SQLite.

AC1: an uploaded template whose boxes are all linked can be used by judges.
AC2: saving with a box unlinked says which box.
AC3: only a SystemAdministrator can manage templates.

DIAAT-346: the product team previews a template with sample details before any judge
sees it, and is told in plain words when the preview fails.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import (
    add_jurisdiction,
    add_member,
    add_user,
    client_as,
    jurisdiction_route_app,
)
from tests.unit.template_docx import JUDGMENT, Sdt, document_text, make_template
from transcribe_api.documents.content_loader import save_document_content
from transcribe_api.domain.models_dictation import AuditLog, JurisdictionTemplate, Transcription, TranscriptionJob

pytestmark = pytest.mark.real_jurisdictions

TEMPLATE = make_template(JUDGMENT)
OPTIONAL = ("Party2", "Party2Title", "Party2Rep")


@pytest.fixture
def admin_app(jurisdiction_db, monkeypatch):
    from transcribe_api.api import routes_dictation_privileged

    monkeypatch.setattr(routes_dictation_privileged, "get_engine", lambda: jurisdiction_db)
    app = FastAPI()
    app.include_router(routes_dictation_privileged.router, prefix="/api")
    return app


@pytest.fixture
def et(jurisdiction_db):
    return add_jurisdiction(jurisdiction_db, "ET")


@pytest.fixture
def as_admin(jurisdiction_db, admin_app, et):
    admin = add_user(jurisdiction_db, "admin@example.test", roles=("SystemAdministrator",))
    return client_as(admin_app, jurisdiction_db, admin)


@pytest.fixture
def judge_app(jurisdiction_db, monkeypatch):
    from transcribe_api.api import routes_dictation, routes_jurisdictions

    monkeypatch.setattr(routes_jurisdictions, "get_engine", lambda: jurisdiction_db)
    monkeypatch.setattr(routes_dictation, "get_engine", lambda: jurisdiction_db)
    return jurisdiction_route_app()


@pytest.fixture
def as_et_judge(jurisdiction_db, judge_app, et):
    judge = add_user(jurisdiction_db, "judge@example.test")
    add_member(jurisdiction_db, judge, et)
    client = client_as(judge_app, jurisdiction_db, judge)
    client.headers["X-Jurisdiction"] = "ET"
    return client


def upload(client, data=TEMPLATE, name="ET judgment.docx", **params):
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    return client.post(
        f"/api/admin/jurisdictions/ET/templates{query}",
        content=data,
        headers={"Content-Type": "application/octet-stream", "X-Upload-Filename": urllib.parse.quote(name)},
    )


def complete(template: dict, **changes) -> dict:
    """The admin's save: the proposed fields, Party 2 optional, with ``changes``."""
    fields = [{**f, "required": False} if f["tag"] in OPTIONAL else f for f in template["fields"]]
    return {
        "name": template["name"],
        "service": None,
        "fields": fields,
        "enabled": True,
        "version": template["version"],
        **changes,
    }


def _audit(engine) -> list[tuple[str, dict]]:
    with Session(engine) as session:
        return [(r.event_type, r.detail) for r in session.exec(select(AuditLog)).all()]


# The admin area ---------------------------------------------------------------------


def test_an_upload_lists_the_templates_boxes_as_fields(jurisdiction_db, as_admin):
    response = upload(as_admin)

    assert response.status_code == 201
    template = response.json()
    assert (template["name"], template["file_name"], template["enabled"], template["problems"]) == (
        "ET judgment",
        "ET judgment.docx",
        False,  # not for judges until the admin has checked it
        [],
    )
    fields = {f["tag"]: f for f in template["fields"]}
    assert list(fields) == [b["key"] for b in template["boxes"]]
    assert fields["Party1Title"] == {
        "tag": "Party1Title",
        "label": "Party1Title",
        "type": "dropdown",
        "required": True,
        "options": ["Claimant", "Respondent"],
        "source": None,
    }
    assert fields["JudgmentText"]["type"] == "dictated"
    assert as_admin.get("/api/admin/jurisdictions/ET/templates").json() == [template]
    (action, detail), *_ = _audit(jurisdiction_db)
    assert (action, detail) == ("template.add", {"code": "ET", "change": "uploaded", "boxes": 10})


def test_a_second_upload_with_the_same_name_gets_a_name_of_its_own(as_admin):
    upload(as_admin)
    assert upload(as_admin).json()["name"] == "ET judgment (2)"


def test_saving_with_a_box_unlinked_names_the_box(as_admin):
    template = upload(as_admin).json()
    body = complete(template)
    body["fields"] = [f for f in body["fields"] if f["tag"] != "Party2Rep"]

    response = as_admin.put(f"/api/admin/jurisdictions/ET/templates/{template['id']}", json=body)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "template_not_ready"
    assert detail["problems"] == ["The box “Party2Rep” (Party2Rep) has no field linked to it."]
    assert as_admin.get("/api/admin/jurisdictions/ET/templates").json() == [template]  # nothing saved


def test_a_field_cannot_be_added_and_a_dropdown_needs_options(as_admin):
    template = upload(as_admin).json()
    body = complete(template)
    body["fields"] = [
        {**f, "type": "dropdown", "options": []} if f["tag"] == "Reference" else f for f in body["fields"]
    ] + [{"tag": "Costs", "label": "Costs", "type": "text", "required": False}]

    detail = as_admin.put(f"/api/admin/jurisdictions/ET/templates/{template['id']}", json=body).json()["detail"]

    assert detail["problems"] == [
        "“Reference” needs at least one option.",
        "“Costs” (Costs) is not a box in the template: fields cannot be added.",
    ]


def test_the_admin_relabels_retypes_and_reorders_fields_and_switches_it_on(as_admin):
    template = upload(as_admin).json()
    body = complete(template, name="Judgment", service="England and Wales")
    fields = [{**f, "label": "Date of hearing"} if f["tag"] == "HearingDate" else f for f in reversed(body["fields"])]
    body["fields"] = [{**f, "options": ["stray"]} if f["tag"] == "Reference" else f for f in fields]

    saved = as_admin.put(f"/api/admin/jurisdictions/ET/templates/{template['id']}", json=body)

    assert saved.status_code == 200
    result = saved.json()
    assert (result["name"], result["service"], result["enabled"], result["problems"]) == (
        "Judgment",
        "England and Wales",
        True,
        [],
    )
    assert [f["tag"] for f in result["fields"]] == [f["tag"] for f in reversed(template["fields"])]
    by_tag = {f["tag"]: f for f in result["fields"]}
    assert by_tag["HearingDate"]["label"] == "Date of hearing"
    assert by_tag["Reference"]["options"] == []  # a text field has no options


def test_a_save_from_a_stale_form_is_refused(as_admin):
    template = upload(as_admin).json()
    path = f"/api/admin/jurisdictions/ET/templates/{template['id']}"
    assert as_admin.put(path, json=complete(template)).status_code == 200
    stale = as_admin.put(path, json=complete(template, name="Other"))
    assert stale.status_code == 409


def test_a_new_file_keeps_the_linked_fields_and_waits_for_its_new_boxes(as_admin):
    template = upload(as_admin).json()
    path = f"/api/admin/jurisdictions/ET/templates/{template['id']}"
    relabelled = complete(template)
    relabelled["fields"] = [
        {**f, "label": "Case number"} if f["tag"] == "Reference" else f for f in relabelled["fields"]
    ]
    enabled = as_admin.put(path, json=relabelled).json()

    new_file = make_template([*JUDGMENT, ["Costs: ", Sdt("Costs")]])
    stale = upload(as_admin, new_file, "v2.docx", replace=template["id"], version=template["version"])
    assert stale.status_code == 409
    replaced = upload(as_admin, new_file, "v2.docx", replace=template["id"], version=enabled["version"])

    assert replaced.status_code == 200
    result = replaced.json()
    assert (result["file_name"], result["enabled"]) == ("v2.docx", False)  # off until complete
    assert result["problems"] == ["The box “Costs” (Costs) has no field linked to it."]
    assert {f["tag"]: f["label"] for f in result["fields"]}["Reference"] == "Case number"
    assert len(as_admin.get("/api/admin/jurisdictions/ET/templates").json()) == 1


def test_a_long_names_copy_keeps_its_number_within_the_limit(as_admin):
    name = "J" * 210 + ".docx"
    first = upload(as_admin, name=name).json()["name"]
    second = upload(as_admin, name=name).json()["name"]
    assert len(first) == len(second) == 200
    assert second.endswith(" (2)")


def test_a_template_is_deleted(jurisdiction_db, as_admin):
    template = upload(as_admin).json()
    assert as_admin.delete(f"/api/admin/jurisdictions/ET/templates/{template['id']}").status_code == 204
    assert as_admin.get("/api/admin/jurisdictions/ET/templates").json() == []
    assert ("template.delete", {"code": "ET"}) in _audit(jurisdiction_db)


@pytest.mark.parametrize(
    ("headers", "data", "status"),
    [
        ({"Content-Type": "multipart/form-data; boundary=x", "X-Upload-Filename": "t.docx"}, TEMPLATE, 415),
        ({"Content-Type": "application/octet-stream", "X-Upload-Filename": "t.pdf"}, TEMPLATE, 400),
        (
            {"Content-Type": "application/octet-stream", "X-Upload-Filename": "t.docx"},
            b"x" * (2 * 1024 * 1024 + 1),
            413,
        ),
        ({"Content-Type": "application/octet-stream", "X-Upload-Filename": "t.docx"}, b"not a zip", 400),
    ],
    ids=["multipart", "not-docx-name", "too-large", "not-a-docx"],
)
def test_only_a_raw_docx_within_the_limit_is_taken(jurisdiction_db, as_admin, headers, data, status):
    response = as_admin.post("/api/admin/jurisdictions/ET/templates", content=data, headers=headers)
    assert response.status_code == status
    with Session(jurisdiction_db) as session:
        assert session.exec(select(JurisdictionTemplate)).all() == []


def test_a_file_benchnotes_cannot_fill_says_why(as_admin):
    response = upload(as_admin, make_template([["No boxes here"]]))
    assert (response.status_code, response.json()["detail"]) == (
        400,
        "The document has no fill-in boxes (content controls).",
    )


@pytest.mark.parametrize("roles", [("Judge",), ("LegalTextManager",)])
def test_only_a_system_administrator_may_manage_templates(jurisdiction_db, admin_app, et, roles):
    user = add_user(jurisdiction_db, roles=roles)
    add_member(jurisdiction_db, user, et, role="admin")  # even an admin of the jurisdiction
    client = client_as(admin_app, jurisdiction_db, user)
    some_id = "00000000-0000-4000-8000-000000000000"
    assert client.get("/api/admin/jurisdictions/ET/templates").status_code == 403
    assert upload(client).status_code == 403
    assert client.put(f"/api/admin/jurisdictions/ET/templates/{some_id}", json={}).status_code == 403
    assert client.delete(f"/api/admin/jurisdictions/ET/templates/{some_id}").status_code == 403
    preview = client.post(f"/api/admin/jurisdictions/ET/templates/{some_id}/preview", json={"version": ""})
    assert preview.status_code == 403
    assert client.get(f"/api/admin/jurisdictions/ET/templates/{some_id}/form").status_code == 403
    with Session(jurisdiction_db) as session:
        assert session.exec(select(JurisdictionTemplate)).all() == []


# Judges --------------------------------------------------------------------------------


def _enabled_template(as_admin, **changes) -> dict:
    template = upload(as_admin).json()
    return as_admin.put(
        f"/api/admin/jurisdictions/ET/templates/{template['id']}", json=complete(template, **changes)
    ).json()


def test_judges_see_an_enabled_template_with_the_fields_they_fill(jurisdiction_db, as_admin, as_et_judge, et):
    save_document_content(
        et.id,
        {
            "legal_frameworks": [
                {"id": "wages", "title": "Unlawful deductions", "rank": 2},
                {"id": "unfair", "title": "Unfair dismissal", "rank": 1},
            ]
        },
    )
    draft = upload(as_admin).json()
    assert as_et_judge.get("/api/jurisdictions/templates").json() == []  # not enabled

    template = upload(as_admin, name="Issues.docx").json()
    body = complete(template)
    body["fields"] = [{**f, "type": "legal_issues"} if f["tag"] == "Party2Rep" else f for f in body["fields"]]
    as_admin.put(f"/api/admin/jurisdictions/ET/templates/{template['id']}", json=body)

    (available,) = as_et_judge.get("/api/jurisdictions/templates").json()
    assert (available["id"], available["name"]) == (template["id"], "Issues")
    fields = {f["tag"]: f for f in available["fields"]}
    # BenchNotes fills the jurisdiction, the year and the judgment: the judge is not asked.
    assert set(fields) == {"Reference", "HearingDate", "Party1", "Party1Title", "Party2", "Party2Title", "Party2Rep"}
    assert fields["Party1Title"]["options"] == [
        {"value": "Claimant", "label": "Claimant"},
        {"value": "Respondent", "label": "Respondent"},
    ]
    assert fields["Party2Rep"]["options"] == [
        {"value": "unfair", "label": "Unfair dismissal"},
        {"value": "wages", "label": "Unlawful deductions"},
    ]
    assert draft["id"] != available["id"]


def test_judges_of_another_jurisdiction_never_see_it(jurisdiction_db, as_admin, judge_app):
    _enabled_template(as_admin)
    iac = add_jurisdiction(jurisdiction_db, "IAC")
    judge = add_user(jurisdiction_db)
    add_member(jurisdiction_db, judge, iac)
    client = client_as(judge_app, jurisdiction_db, judge)
    assert client.get("/api/jurisdictions/templates", headers={"X-Jurisdiction": "IAC"}).json() == []


@pytest.fixture
def stored(mocker):
    """The documents the submission stores, by blob path."""
    blobs: dict[str, bytes] = {}

    async def create(path, blob_path):
        blobs[blob_path] = Path(path).read_bytes()
        return True

    manager = MagicMock()
    manager.create_blob_from_file = AsyncMock(side_effect=create)
    manager.set_blob_metadata = AsyncMock(return_value=True)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=manager)
    context.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("transcribe_api.api.routes_dictation.AsyncAzureBlobManager", return_value=context)
    mocker.patch(
        "transcribe_api.api.routes_dictation.get_settings",
        return_value=MagicMock(DIRECT_SDK_ACCESS=False, APP_URL="https://benchnotes.test"),
    )
    return blobs


def _submission(template_id: str, values: dict) -> dict:
    return {
        "template_id": template_id,
        "values": values,
        "messages": {"background": [{"speaker": "Judge", "text": "The claim succeeds.", "timestamp": "00:01"}]},
        "notes": "<p>Costs are reserved.</p>",
    }


VALUES = {"Reference": "1234/2026", "HearingDate": "2026-02-12", "Party1": "A Smith", "Party1Title": "Claimant"}


def test_a_judge_gets_a_document_from_the_template_with_no_release(jurisdiction_db, as_admin, as_et_judge, stored):
    template = _enabled_template(as_admin)

    response = as_et_judge.post("/api/live-transcription/submission", json=_submission(template["id"], VALUES))

    assert response.status_code == 200, response.text
    document = stored[response.json()["document_blob_path"]]
    assert document_text(document) == [
        "Jurisdiction: ET name",
        "Case Number: 1234/2026",
        "Heard on 12 February 2026",
        "A Smith (Claimant)",
        "The claim succeeds.\n\nCosts are reserved.",
        f"© Crown copyright {document_year()}",
    ]
    submission = json.loads(stored[response.json()["blob_path"]])
    assert submission["form_data"] == {"template_id": template["id"], "template_name": "ET judgment", "values": VALUES}
    with Session(jurisdiction_db) as session:
        transcription = session.get(Transcription, UUID(response.json()["transcription_id"]))
        assert transcription.title == "ET judgment: 1234/2026"
        job = session.exec(select(TranscriptionJob)).one()
    with Session(jurisdiction_db) as session:
        row = session.get(JurisdictionTemplate, UUID(template["id"]))
    assert (job.provenance["template"], job.provenance["template_version"]) == (
        "uploaded",
        hashlib.sha256(row.content).hexdigest()[:12],
    )


def document_year() -> int:
    from datetime import UTC, datetime

    return datetime.now(UTC).year


def test_values_that_do_not_fit_the_template_are_refused(as_admin, as_et_judge, stored):
    template = _enabled_template(as_admin)
    response = as_et_judge.post(
        "/api/live-transcription/submission", json=_submission(template["id"], {**VALUES, "Party1Title": "Judge"})
    )
    assert response.status_code == 400
    assert response.json()["detail"]["problems"] == ["“Party1Title” has a choice that is not one of its options."]
    assert stored == {}


def test_a_template_judges_cannot_choose_is_not_used(as_admin, as_et_judge, stored):
    draft = upload(as_admin).json()
    response = as_et_judge.post("/api/live-transcription/submission", json=_submission(draft["id"], VALUES))
    assert response.status_code == 404
    assert stored == {}


def test_a_submission_is_either_the_decision_form_or_a_template(as_admin, as_et_judge, stored):
    template = _enabled_template(as_admin)
    body = {**_submission(template["id"], VALUES), "form_data": {"location": "x"}}
    assert as_et_judge.post("/api/live-transcription/submission", json=body).status_code == 422
    neither = {"messages": {"background": [], "evidence": [], "facts": []}}
    assert as_et_judge.post("/api/live-transcription/submission", json=neither).status_code == 422
    no_values = {k: v for k, v in _submission(template["id"], VALUES).items() if k != "values"}
    assert as_et_judge.post("/api/live-transcription/submission", json=no_values).status_code == 422


# Previewing (DIAAT-346) ---------------------------------------------------------------


def preview(client, template: dict, values: dict, version: str | None = None):
    return client.post(
        f"/api/admin/jurisdictions/ET/templates/{template['id']}/preview",
        json={"values": values, "version": template["version"] if version is None else version},
    )


def test_a_template_is_previewed_as_a_judge_would_get_it_before_judges_see_it(as_admin, as_et_judge):
    template = upload(as_admin).json()
    saved = as_admin.put(
        f"/api/admin/jurisdictions/ET/templates/{template['id']}", json=complete(template, enabled=False)
    ).json()
    assert as_et_judge.get("/api/jurisdictions/templates").json() == []  # judges cannot choose it yet

    response = preview(as_admin, saved, VALUES)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''Preview%20-%20ET%20judgment.docx"
    # The judge's document (test_a_judge_gets_a_document_from_the_template_with_no_release),
    # with sample text where the dictation goes.
    assert document_text(response.content) == [
        "Jurisdiction: ET name",
        "Case Number: 1234/2026",
        "Heard on 12 February 2026",
        "A Smith (Claimant)",
        (
            "This is where the judge's dictated text goes.\n\n"
            "Each paragraph the judge dictates or types appears here, in this box's style."
        ),
        f"© Crown copyright {document_year()}",
    ]


def test_the_sample_details_form_is_the_judges_before_judges_can_choose_it(as_admin, et):
    save_document_content(et.id, {"legal_frameworks": [{"id": "unfair", "title": "Unfair dismissal", "rank": 1}]})
    template = upload(as_admin).json()
    body = complete(template, enabled=False)
    body["fields"] = [{**f, "type": "legal_issues"} if f["tag"] == "Party2Rep" else f for f in body["fields"]]
    as_admin.put(f"/api/admin/jurisdictions/ET/templates/{template['id']}", json=body)

    response = as_admin.get(f"/api/admin/jurisdictions/ET/templates/{template['id']}/form")

    assert response.status_code == 200
    form = response.json()
    assert (form["id"], form["name"]) == (template["id"], "ET judgment")
    fields = {f["tag"]: f for f in form["fields"]}
    # As in test_judges_see_an_enabled_template_with_the_fields_they_fill.
    assert set(fields) == {"Reference", "HearingDate", "Party1", "Party1Title", "Party2", "Party2Title", "Party2Rep"}
    assert fields["Party2Rep"]["options"] == [{"value": "unfair", "label": "Unfair dismissal"}]
    unknown = "00000000-0000-4000-8000-000000000000"
    assert as_admin.get(f"/api/admin/jurisdictions/ET/templates/{unknown}/form").status_code == 404


def test_a_preview_of_an_incomplete_template_says_what_is_missing(as_admin):
    template = upload(as_admin).json()
    new_file = make_template([*JUDGMENT, ["Costs: ", Sdt("Costs")]])
    replaced = upload(as_admin, new_file, "v2.docx", replace=template["id"], version=template["version"]).json()

    response = preview(as_admin, replaced, VALUES)

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "template_not_ready",
        "message": "The template cannot be previewed yet. The box “Costs” (Costs) has no field linked to it.",
        "problems": ["The box “Costs” (Costs) has no field linked to it."],
    }


def test_sample_details_that_do_not_fit_say_which(as_admin):
    template = _enabled_template(as_admin)

    response = preview(as_admin, template, {**VALUES, "Party1Title": "Judge", "HearingDate": "", "Extra": "x"})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "template_values_invalid"
    assert detail["problems"] == [
        "“Extra” is not a field of this template.",
        "“Hearing date” is required.",
        "“Party1Title” has a choice that is not one of its options.",
    ]
    assert detail["message"].startswith("The hearing details do not fit the template. ")


def test_a_preview_of_a_template_changed_meanwhile_is_refused(as_admin):
    template = _enabled_template(as_admin)
    assert preview(as_admin, template, VALUES, version="2020-01-01T00:00:00").status_code == 409


def test_a_file_that_cannot_be_filled_is_explained(as_admin, mocker):
    template = _enabled_template(as_admin)
    mocker.patch("transcribe_api.jurisdiction_templates.fill_template", side_effect=KeyError("rId9"))

    response = preview(as_admin, template, VALUES)

    assert (response.status_code, response.json()["detail"]) == (
        400,
        (
            "BenchNotes could not fill this Word file, so judges would not get a document from it either. "
            "Check that it opens in Word, then upload it again."
        ),
    )


def test_a_local_web_app_may_send_the_files_name(monkeypatch):
    """Local development calls the API from another origin: the preflight must allow it."""
    from fastapi.testclient import TestClient

    from transcribe_api.api.app import create_app

    monkeypatch.setenv("ENVIRONMENT", "local")
    response = TestClient(create_app()).options(
        "/api/admin/jurisdictions/ET/templates",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-upload-filename",
        },
    )
    assert response.status_code == 200
    assert "x-upload-filename" in response.headers["access-control-allow-headers"].lower()
