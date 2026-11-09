"""DIAAT-342: each jurisdiction's admins edit their own jurisdiction's legal wording
(hearing types, legal frameworks, anonymity text), and only it.

Real lookups, routes, content loader and document renderer, on SQLite (the real
PostgreSQL behaviour is in test_jurisdiction_legal_wording_postgres.py).
"""

from __future__ import annotations

import json
import re
import threading
import zipfile
from pathlib import Path

import pytest
from docx import Document
from fastapi import FastAPI
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import (
    IAC_JURISDICTION_ID,
    add_iac_as_migrated,
    add_jurisdiction,
    add_member,
    add_user,
    client_as,
)
from transcribe_api.documents import content_loader
from transcribe_api.domain.models_dictation import AuditLog, DocumentContent

pytestmark = pytest.mark.real_jurisdictions

BUNDLED = json.loads(
    (Path(__file__).parents[2] / "src" / "transcribe_api" / "data" / "document_content.json").read_text()
)
TEMPLATE = {"template": "iac_decision"}
ET_FRAMEWORK = {
    "id": "et_unfair_dismissal",
    "title": "Unfair dismissal",
    "rank": 1,
    "content": "ZEBRA-FRAMEWORK-TEXT: the statutory test for unfair dismissal.",
    "issues": ["ZEBRA-ISSUE: was the reason for dismissal a fair one?"],
}
IAC = {"X-Jurisdiction": "IAC"}
ET = {"X-Jurisdiction": "ET"}


def _app() -> FastAPI:
    from tests.unit.jurisdiction_fixtures import jurisdiction_route_app
    from transcribe_api.api.routes_dictation_privileged import router as admin_router

    app = jurisdiction_route_app()
    app.include_router(admin_router, prefix="/api")
    return app


@pytest.fixture
def world(jurisdiction_db):
    """The IAC and the ET (both on the decision template), the SSCS (no template), and a
    user of each kind."""
    engine = jurisdiction_db
    iac = add_iac_as_migrated(engine)
    et = add_jurisdiction(engine, "ET", settings=TEMPLATE)
    sscs = add_jurisdiction(engine, "SSCS")

    class World:
        pass

    w = World()
    w.engine, w.iac, w.et, w.sscs = engine, iac, et, sscs

    # The IAC's LegalTextManager (DIAAT-373: a role held in a jurisdiction, no longer a global Entra one).
    w.ltm = add_user(engine)
    add_member(engine, w.ltm, iac, role="LegalTextManager")
    # A SystemAdministrator (the Entra role), a judge in the IAC.
    w.sysadmin = add_user(engine, roles=("Judge", "SystemAdministrator"))
    add_member(engine, w.sysadmin, iac)
    # An administrator of the IAC.
    w.iac_admin = add_user(engine)
    add_member(engine, w.iac_admin, iac, role="admin")
    # An admin of the ET who is only a judge in the IAC.
    w.et_admin = add_user(engine)
    add_member(engine, w.et_admin, iac)
    add_member(engine, w.et_admin, et, role="admin")
    # An ET judge, and the ET's LegalTextManager.
    w.et_judge = add_user(engine)
    add_member(engine, w.et_judge, et)
    w.ltm_et_only = add_user(engine)
    add_member(engine, w.ltm_et_only, et, role="LegalTextManager")
    # An IAC judge, and an admin of the SSCS (which has no decision template).
    w.iac_judge = add_user(engine)
    add_member(engine, w.iac_judge, iac)
    w.sscs_admin = add_user(engine)
    add_member(engine, w.sscs_admin, sscs, role="admin")
    return w


def client(world, user):
    return client_as(_app(), world.engine, user)


def row(world, jurisdiction) -> DocumentContent | None:
    with Session(world.engine) as session:
        return session.exec(select(DocumentContent).where(DocumentContent.jurisdiction_id == jurisdiction.id)).first()


def _iac_row_snapshot(world):
    stored = row(world, world.iac)
    return None if stored is None else (stored.id, stored.content, stored.updated_at)


def seed_iac(world):
    """The IAC's row, as the first read after a deployment seeds it."""
    content_loader.get_document_content(IAC_JURISDICTION_ID)
    return _iac_row_snapshot(world)


# ---------------------------------------------------------------------------
# AC1: the editor shows only the admin's own jurisdiction's legal wording
# ---------------------------------------------------------------------------


def test_an_et_admin_sees_the_ets_legal_wording_not_the_iacs(world):
    body = client(world, world.et_admin).get("/api/admin/document-content", headers=ET).json()
    assert body["jurisdiction"] == {"code": "ET", "name": "ET name"}
    # A new jurisdiction starts with no legal frameworks, and the hearing types and
    # anonymity text a document needs.
    assert body["legal_frameworks"] == []
    assert body["has_legal_wording"] is False
    assert body["hearing_types"] == BUNDLED["hearing_types"]
    assert body["anonymity"] == BUNDLED["anonymity"]


def test_an_iac_admin_member_sees_the_iacs_legal_wording(world):
    body = client(world, world.iac_admin).get("/api/admin/document-content", headers=IAC).json()
    assert body["jurisdiction"]["code"] == "IAC"
    assert body["has_legal_wording"] is True
    assert body["legal_frameworks"] == BUNDLED["legal_frameworks"]


def test_the_editor_follows_the_tab_s_jurisdiction(world):
    """An ET admin who is also an IAC judge sees the ET's in an ET tab, and nothing in an IAC tab."""
    c = client(world, world.et_admin)
    assert c.get("/api/admin/document-content", headers=ET).json()["jurisdiction"]["code"] == "ET"
    refused = c.get("/api/admin/document-content", headers=IAC)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "jurisdiction_admin_required"


# ---------------------------------------------------------------------------
# AC3: no reading or changing another jurisdiction's legal wording, either way
# ---------------------------------------------------------------------------


def _every_request(c, headers):
    """Every admin legal wording route, with a valid body, as (response)."""
    hearing = {"id": "face_to_face", "title": "T", "hearing_title": "T", "hearing_description": "D"}
    framework = {"id": "x", "title": "T", "rank": 1, "content": "C", "issues": []}
    status = {
        "id": "granted",
        "title": "T",
        "content": "C",
        "identifier": "",
        "header": "H",
        "order_header": "O",
        "order_text": "T",
    }
    return {
        "read": c.get("/api/admin/document-content", headers=headers),
        "put hearing type": c.put("/api/admin/document-content/hearing-types/face_to_face", json=hearing, headers=headers),
        "post hearing type": c.post("/api/admin/document-content/hearing-types", json={**hearing, "id": "n"}, headers=headers),
        "delete hearing type": c.delete("/api/admin/document-content/hearing-types/face_to_face", headers=headers),
        "put framework": c.put("/api/admin/document-content/legal-frameworks/asylum_pre_naba", json={**framework, "id": "asylum_pre_naba"}, headers=headers),
        "post framework": c.post("/api/admin/document-content/legal-frameworks", json=framework, headers=headers),
        "delete framework": c.delete("/api/admin/document-content/legal-frameworks/asylum_pre_naba", headers=headers),
        "put anonymity": c.put("/api/admin/document-content/anonymity-statuses/granted", json=status, headers=headers),
    }


def test_an_et_admin_cannot_read_or_change_the_iacs_legal_wording(world):
    before = seed_iac(world)
    c = client(world, world.et_admin)  # a judge in the IAC, admin of the ET
    for name, response in _every_request(c, IAC).items():
        assert response.status_code == 403, name
        assert response.json()["detail"]["code"] == "jurisdiction_admin_required", name
    assert _iac_row_snapshot(world) == before


def test_an_iac_admin_cannot_read_or_change_the_ets_legal_wording(world):
    """The IAC's admin does not work in the ET: a tab naming it is out of step with their token's
    context (the IAC), for every route (409: the tab reloads, nothing is changed)."""
    for user in (world.iac_admin, world.ltm):
        c = client(world, user)
        for name, response in _every_request(c, ET).items():
            assert response.status_code == 409, name
            assert response.json()["detail"]["code"] == "context_changed", name
    assert row(world, world.et) is None


def test_a_legal_text_manager_has_a_say_in_their_own_jurisdiction_only(world):
    """DIAAT-373: the role is held in a jurisdiction (it was a global Entra role that meant the
    IAC). The ET's can edit the ET's legal wording, and nothing in the IAC."""
    c = client(world, world.ltm_et_only)
    assert c.get("/api/admin/document-content", headers=ET).status_code == 200
    assert c.post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=ET).status_code == 201
    for name, response in _every_request(c, IAC).items():
        assert response.status_code == 409, name
        assert response.json()["detail"]["code"] == "context_changed", name
    assert [f["id"] for f in row(world, world.et).content["legal_frameworks"]] == ["et_unfair_dismissal"]


def test_a_legal_text_manager_in_the_iac_has_no_say_in_the_et(world):
    for name, response in _every_request(client(world, world.ltm), ET).items():
        assert response.status_code == 409, name
    assert row(world, world.et) is None


def test_an_et_judge_cannot_edit_and_an_iac_judge_cannot_edit(world):
    for user, headers in ((world.et_judge, ET), (world.iac_judge, IAC)):
        for name, response in _every_request(client(world, user), headers).items():
            assert response.status_code == 403, name
            assert response.json()["detail"]["code"] == "jurisdiction_admin_required", name


def test_a_refusal_is_audited_without_the_wording(world):
    client(world, world.et_judge).get("/api/admin/document-content", headers=ET)
    with Session(world.engine) as session:
        [denied] = session.exec(select(AuditLog).where(AuditLog.event_type == "ACCESS_DENIED")).all()
    assert denied.outcome == "denied"
    assert denied.resource == "GET /api/admin/document-content"


def test_a_jurisdiction_without_the_decision_template_has_no_legal_wording_to_edit(world):
    response = client(world, world.sscs_admin).get("/api/admin/document-content", headers={"X-Jurisdiction": "SSCS"})
    assert (response.status_code, response.json()) == (404, {"detail": "Not available in this jurisdiction"})


# ---------------------------------------------------------------------------
# AC5: IAC admins (and its LegalTextManager) work as before
# ---------------------------------------------------------------------------


def test_a_legal_text_manager_edits_the_iacs_legal_wording_as_before(world):
    c = client(world, world.ltm)
    seeded = seed_iac(world)
    assert seeded[0] == 1  # the IAC's row is still id 1

    new = {"id": "face_to_face", "title": "In person", "hearing_title": "The Hearing", "hearing_description": "Edited."}
    assert c.put("/api/admin/document-content/hearing-types/face_to_face", json=new, headers=IAC).status_code == 200
    created = c.post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK | {"id": "added"}, headers=IAC)
    assert created.status_code == 201
    assert c.delete("/api/admin/document-content/legal-frameworks/asylum_pre_naba", headers=IAC).status_code == 204

    stored = row(world, world.iac)
    assert stored.id == 1
    assert next(h for h in stored.content["hearing_types"] if h["id"] == "face_to_face")["title"] == "In person"
    ids = [f["id"] for f in stored.content["legal_frameworks"]]
    assert "added" in ids and "asylum_pre_naba" not in ids
    # The routes without the header work for the IAC-only user too (a user with one jurisdiction).
    assert c.get("/api/admin/document-content").status_code == 200
    # Nothing was created for anyone else.
    assert row(world, world.et) is None


def test_the_other_roles_keep_what_they_had_on_the_iac(world):
    # A SystemAdministrator reads (as before) but never wrote with that role alone.
    sysadmin = client(world, world.sysadmin)
    assert sysadmin.get("/api/admin/document-content", headers=IAC).status_code == 200
    denied = sysadmin.post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=IAC)
    assert denied.status_code == 403
    # An admin member of the IAC without the global role can now edit its wording too.
    iac_admin = client(world, world.iac_admin)
    assert iac_admin.post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=IAC).status_code == 201


def test_a_system_administrator_has_no_special_access_to_another_jurisdiction(world):
    """SystemAdministrator does not work in the ET and so cannot even name it."""
    for name, response in _every_request(client(world, world.sysadmin), ET).items():
        assert response.status_code == 409, name
        assert response.json()["detail"]["code"] == "context_changed", name


# ---------------------------------------------------------------------------
# AC2 and isolation: a save is what the next judge reads and the next document uses
# ---------------------------------------------------------------------------


def test_an_et_admin_s_save_is_the_ets_alone(world):
    iac_before = seed_iac(world)
    c = client(world, world.et_admin)
    assert c.post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=ET).status_code == 201

    stored = row(world, world.et)
    assert [f["id"] for f in stored.content["legal_frameworks"]] == ["et_unfair_dismissal"]
    assert stored.content["hearing_types"] == BUNDLED["hearing_types"]
    assert stored.id != 1  # id 1 is the IAC's
    assert _iac_row_snapshot(world) == iac_before  # untouched: content and updated_at
    # The admin read says legal wording is now on.
    body = c.get("/api/admin/document-content", headers=ET).json()
    assert body["has_legal_wording"] is True
    assert [f["id"] for f in body["legal_frameworks"]] == ["et_unfair_dismissal"]


def test_each_jurisdiction_s_judge_gets_their_own_wording_and_the_flag(world):
    client(world, world.et_admin).post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=ET)
    seed_iac(world)

    et_judge = client(world, world.et_judge).get("/api/document-content", headers=ET).json()
    assert et_judge["has_legal_wording"] is True
    assert [f["id"] for f in et_judge["legal_frameworks"]] == ["et_unfair_dismissal"]

    iac_judge = client(world, world.iac_judge).get("/api/document-content", headers=IAC).json()
    assert iac_judge["has_legal_wording"] is True
    assert iac_judge["legal_frameworks"] == BUNDLED["legal_frameworks"]


def test_a_judge_without_legal_wording_gets_the_flag_off_and_no_frameworks(world):
    body = client(world, world.et_judge).get("/api/document-content", headers=ET).json()
    assert body["has_legal_wording"] is False
    assert body["legal_frameworks"] == []
    assert body["hearing_types"] == BUNDLED["hearing_types"]  # needed to render a document


def test_deleting_the_last_framework_turns_legal_wording_off_and_adding_one_turns_it_on(world):
    c = client(world, world.et_admin)
    c.post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=ET)
    judge = client(world, world.et_judge)
    assert judge.get("/api/document-content", headers=ET).json()["has_legal_wording"] is True
    assert c.delete("/api/admin/document-content/legal-frameworks/et_unfair_dismissal", headers=ET).status_code == 204
    assert judge.get("/api/document-content", headers=ET).json()["has_legal_wording"] is False


def test_a_change_is_audited_by_item_and_never_carries_the_wording(world):
    client(world, world.et_admin).post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=ET)
    with Session(world.engine) as session:
        [edit] = session.exec(select(AuditLog).where(AuditLog.event_type == "jurisdiction.edit")).all()
    assert edit.outcome == "success"
    assert edit.detail == {
        "code": "ET",
        "fields": ["legal_wording"],
        "change": "created legal framework",
        "item": "et_unfair_dismissal",
    }
    assert "ZEBRA" not in json.dumps(edit.detail)


def test_the_cache_is_per_jurisdiction_and_follows_a_save(world):
    seed_iac(world)
    first = content_loader.get_document_content(world.et.id)
    assert first["legal_frameworks"] == []
    client(world, world.et_admin).post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=ET)
    assert [f["id"] for f in content_loader.get_document_content(world.et.id)["legal_frameworks"]] == [
        "et_unfair_dismissal"
    ]
    assert content_loader.get_document_content(IAC_JURISDICTION_ID)["legal_frameworks"] == BUNDLED["legal_frameworks"]


def test_only_the_iac_falls_back_to_the_bundled_file_when_the_database_is_down(world, monkeypatch):
    from transcribe_api.runtime import db_connection

    def down():
        msg = "database down"
        raise RuntimeError(msg)

    monkeypatch.setattr(db_connection, "get_engine", down)
    content_loader.clear_cache()
    assert content_loader.get_document_content(IAC_JURISDICTION_ID) == BUNDLED
    with pytest.raises(content_loader.ContentLoadError):
        content_loader.get_document_content(world.et.id)


# ---------------------------------------------------------------------------
# Documents: real .docx files read back
# ---------------------------------------------------------------------------

_W_T = re.compile(r"<w:t(?: [^>]*)?>([^<]*)</w:t>")


def _submission(legal_issues: list[str]):
    from transcribe_api.api.routes_dictation import LiveTranscriptionSubmissionRequest

    form = {
        "caseId": "PA/1/2026",
        "location": "Taylor House",
        "hearingDate": "2026-03-04",
        "judgeName": "A Judge",
        "anonymityOrder": "not_sought",
        "appellantName": "A B",
        "respondent": "Secretary of State",
        "hearingType": "face_to_face",
        "appealableDecisionDate": "2026-01-02",
        "documentType": "decision",
        "jurisdiction": "First-tier Tribunal Immigration and Asylum Chamber",  # free text: never used
        "legalIssues": legal_issues,
    }
    messages = {"background": [{"speaker": "Judge", "text": "Background words.", "timestamp": "10:01"}]}
    return LiveTranscriptionSubmissionRequest.model_validate({"form_data": form, "messages": messages})


def _render(tmp_path, jurisdiction_id, legal_issues):
    from transcribe_api.documents.template_renderer import render_hearing_document

    return render_hearing_document(_submission(legal_issues), tmp_path, "judge@example.test", jurisdiction_id)


def _all_text(path: Path) -> dict[str, list[str]]:
    """Every piece of text in the .docx: body paragraphs, tables, headers and footers (python-docx),
    and the w:t text of every XML part (content controls, text boxes, notes)."""
    document = Document(str(path))
    paragraphs = [p.text for p in document.paragraphs]
    for table in document.tables:
        for cell_row in table.rows:
            for cell in cell_row.cells:
                paragraphs += [p.text for p in cell.paragraphs]
    parts = []
    for section in document.sections:
        for part in (
            section.header, section.footer, section.first_page_header, section.first_page_footer,
            section.even_page_header, section.even_page_footer,
        ):
            parts += [p.text for p in part.paragraphs]
            for table in part.tables:
                for cell_row in table.rows:
                    for cell in cell_row.cells:
                        parts += [p.text for p in cell.paragraphs]
    raw = []
    with zipfile.ZipFile(path) as docx:
        for name in docx.namelist():
            if name.startswith("word/") and name.endswith(".xml"):
                raw += _W_T.findall(docx.read(name).decode("utf-8"))
    return {"paragraphs": paragraphs, "headers_footers": parts, "raw": raw}


def test_after_a_save_the_next_document_uses_the_new_wording(world, tmp_path):
    c = client(world, world.et_admin)
    before = _all_text(_render(tmp_path, world.et.id, ["et_unfair_dismissal"]))
    assert not any("ZEBRA" in t for t in before["raw"])

    assert c.post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=ET).status_code == 201
    hearing = {
        "id": "face_to_face",
        "title": "Face to face",
        "hearing_title": "The Hearing",
        "hearing_description": "QUOKKA-HEARING-TEXT in the ET.",
    }
    assert c.put("/api/admin/document-content/hearing-types/face_to_face", json=hearing, headers=ET).status_code == 200

    after = _all_text(_render(tmp_path, world.et.id, ["et_unfair_dismissal"]))
    assert "Issues" in after["paragraphs"] and "Legal Framework" in after["paragraphs"]
    assert any("ZEBRA-FRAMEWORK-TEXT" in p for p in after["paragraphs"])
    assert any("ZEBRA-ISSUE" in p for p in after["paragraphs"])
    assert any("QUOKKA-HEARING-TEXT" in p for p in after["paragraphs"])

    # The IAC's next document does not: it still has its own wording.
    iac = _all_text(_render(tmp_path, IAC_JURISDICTION_ID, [BUNDLED["legal_frameworks"][0]["id"]]))
    assert not any("ZEBRA" in t or "QUOKKA" in t for t in iac["raw"])
    assert BUNDLED["legal_frameworks"][0]["issues"][0] in " ".join(iac["raw"])


def test_the_documents_wording_is_the_active_jurisdictions_not_the_form_field(world, tmp_path):
    """The form says IAC (as every form does by default); the document is the ET's."""
    client(world, world.et_admin).post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=ET)
    text = _all_text(_render(tmp_path, world.et.id, ["et_unfair_dismissal", BUNDLED["legal_frameworks"][0]["id"]]))
    assert any("ZEBRA-FRAMEWORK-TEXT" in p for p in text["paragraphs"])
    # The IAC's framework, named by a stale form value, is not the ET's: not rendered.
    assert BUNDLED["legal_frameworks"][0]["content"][:40] not in " ".join(text["raw"])


NO_WORDING_ABSENT = (
    "Legal Framework",
    "The parties agreed that the issues",
    "THIS IS BASED ON A STANDARD LIST",
    "[No legal issues selected]",
    "[No legal frameworks selected]",
    "[Legal frameworks not found]",
)


@pytest.mark.parametrize("legal_issues", [[], ["asylum_pre_naba"], ["et_unfair_dismissal", "gone"]])
def test_a_document_without_legal_wording_has_no_issues_or_legal_framework_section(world, tmp_path, legal_issues):
    """The ET has no legal framework: whatever legal issues a form still carries (a draft
    from before, a stale tab), no heading, intro or placeholder of those sections is in
    the body, a table, a header or a footer."""
    text = _all_text(_render(tmp_path, world.et.id, legal_issues))
    everything = text["paragraphs"] + text["headers_footers"] + text["raw"]
    for absent in NO_WORDING_ABSENT:
        assert not any(absent in t for t in everything), absent
    assert "Issues" not in everything  # the heading of the Issues section
    for framework in BUNDLED["legal_frameworks"]:
        assert framework["content"][:40] not in " ".join(everything)
        for issue in framework["issues"]:
            assert issue not in " ".join(everything)
    # Everything else is there.
    for kept in ("Background", "Evidence", "Facts", "Determination of the Issues", "Notice of Decision", "Signature"):
        assert kept in text["paragraphs"], kept
    assert any("Background words." in p for p in text["paragraphs"])


def test_the_document_without_legal_wording_is_the_one_with_it_minus_those_sections(world, tmp_path):
    client(world, world.et_admin).post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=ET)
    with_wording = _all_text(_render(tmp_path, world.et.id, ["et_unfair_dismissal"]))["paragraphs"]
    client(world, world.et_admin).delete("/api/admin/document-content/legal-frameworks/et_unfair_dismissal", headers=ET)
    without = _all_text(_render(tmp_path, world.et.id, ["et_unfair_dismissal"]))["paragraphs"]
    removed = [p for p in with_wording if p not in without]
    assert "Issues" in removed and "Legal Framework" in removed
    assert [p for p in without if p not in with_wording] == []


def test_concurrent_renders_in_two_jurisdictions_do_not_see_each_others_wording(world, tmp_path):
    """The jurisdiction is per render (a context), not per process."""
    client(world, world.et_admin).post("/api/admin/document-content/legal-frameworks", json=ET_FRAMEWORK, headers=ET)
    seed_iac(world)
    seen: dict[str, list[bool]] = {"et": [], "iac": []}
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def render(name, jurisdiction_id, issue):
        try:
            barrier.wait()
            for _ in range(5):
                text = " ".join(_all_text(_render(tmp_path, jurisdiction_id, [issue]))["raw"])
                seen[name].append("ZEBRA-FRAMEWORK-TEXT" in text)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=render, args=("et", world.et.id, "et_unfair_dismissal")),
        threading.Thread(target=render, args=("iac", IAC_JURISDICTION_ID, BUNDLED["legal_frameworks"][0]["id"])),
    ]
    # The in-memory database is one connection: serialise its use, keep the contexts concurrent.
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert seen == {"et": [True] * 5, "iac": [False] * 5}
