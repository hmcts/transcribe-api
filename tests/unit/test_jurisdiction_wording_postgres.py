"""DIAAT-342 end to end on a real Postgres: admin edits, judge's form and the next document.

Needs TEST_POSTGRES_URL (a throwaway Postgres); skipped without one. The database is
migrated to head, the routes run for real (only authentication is replaced), and the
documents are rendered to .docx and read back, so what is asserted is what a judge
would open.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)"
)

IAC_NAME = "First-tier Tribunal Immigration and Asylum Chamber"
TAX_NAME = "Tax Tribunal"
ISSUES_INTRO_START = "The parties agreed that the issues"


@pytest.fixture
def database():
    from transcribe_api.documents import content_loader
    from transcribe_api.domain import interface_dictation
    from transcribe_api.runtime import db_connection, migrations

    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    migrations.run_migrations(engine)
    previous = (db_connection._sync_engine_singleton, interface_dictation.engine)
    # The app reaches the database through get_engine() and, in interface_dictation, its own module-level engine.
    db_connection._sync_engine_singleton = interface_dictation.engine = engine
    content_loader.clear_cache()
    yield engine
    db_connection._sync_engine_singleton, interface_dictation.engine = previous
    content_loader.clear_cache()
    engine.dispose()


def _make_user(engine, roles: list[str], *mapped_to: str):
    from transcribe_api.domain.models_dictation import JurisdictionAdmin, User

    with Session(engine, expire_on_commit=False) as session:
        user = User(
            email=f"{roles[0]}-{uuid.uuid4().hex[:6]}@justice.gov.uk", azure_user_id=uuid.uuid4().hex, role=roles[0]
        )
        session.add(user)
        session.commit()
        for jurisdiction_id in mapped_to:
            session.add(JurisdictionAdmin(user_id=user.id, jurisdiction_id=jurisdiction_id))
        session.commit()
    user.__dict__["app_roles"] = roles
    return user


@pytest.fixture
def api(database):
    import importlib

    from transcribe_api.api import routes_dictation, routes_dictation_privileged
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    # Another test reloads the auth module, after which routers imported earlier hold a stale
    # get_current_user that cannot be overridden by name. Reloading the routers rebinds them.
    importlib.reload(routes_dictation_privileged)
    importlib.reload(routes_dictation)
    app = FastAPI()
    app.include_router(routes_dictation_privileged.router, prefix="/api")
    app.include_router(routes_dictation.router, prefix="/api")
    client = TestClient(app, raise_server_exceptions=False)

    return client, lambda user: app.dependency_overrides.__setitem__(get_current_user, lambda: user)


def _submission(jurisdiction: str | None, legal_issues: list[str]):
    from transcribe_api.api.routes_dictation import LiveTranscriptionSubmissionRequest

    form = {
        "location": "Taylor House",
        "hearingDate": "2026-01-15",
        "judgeName": "N SONI",
        "anonymityOrder": "granted",
        "appellantName": "ELLIOTT",
        "respondent": "THE SECRETARY OF STATE",
        "hearingType": "face_to_face",
        "appealableDecisionDate": "2025-06-01",
        "legalIssues": legal_issues,
        "documentType": "Decision",
    }
    if jurisdiction is not None:
        form["jurisdiction"] = jurisdiction
    return LiveTranscriptionSubmissionRequest.model_validate(
        {
            "form_data": form,
            "messages": {
                "background": [
                    {"speaker": "Judge", "text": "Background text", "timestamp": "00:00:00", "timestampMs": 0}
                ],
                "evidence": [],
                "facts": [],
            },
        }
    )


def _next_document_text(tmp_path: Path, jurisdiction: str | None, legal_issues: list[str]) -> list[str]:
    """Render the document the judge's submission produces now, and return its paragraphs."""
    from transcribe_api.documents.template_renderer import render_hearing_document

    path = render_hearing_document(_submission(jurisdiction, legal_issues), tmp_path / uuid.uuid4().hex, "judge@x.test")
    return [p.text for p in Document(str(path)).paragraphs if p.text]


def _stored_content(engine, jurisdiction_id: str):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT content::text, updated_at FROM document_content WHERE jurisdiction_id = :j"),
            {"j": jurisdiction_id},
        ).first()


FRAMEWORK = {
    "id": "vat",
    "title": "VAT appeals",
    "rank": 1,
    "content": "The VAT framework applies.",
    "issues": ["Was the supply zero-rated?"],
}


def test_an_admin_edit_is_used_by_the_next_document_and_stays_in_their_jurisdiction(api, database, tmp_path):
    from transcribe_api.documents.content_loader import CONTENT_FILE, get_document_content

    client, as_user = api
    sysadmin = _make_user(database, ["SystemAdministrator"])
    iac_admin = _make_user(database, ["LegalTextManager"], "iac")
    tax_admin = _make_user(database, ["LegalTextManager"])

    # IAC content is seeded on first read, exactly the shipped defaults.
    shipped = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))
    assert get_document_content("iac") == shipped
    iac_before = _stored_content(database, "iac")

    # A SystemAdministrator creates a jurisdiction and maps an admin to it.
    as_user(sysadmin)
    assert client.post("/api/admin/jurisdictions", json={"id": "tax", "name": TAX_NAME}).status_code == 201
    assert client.put(f"/api/admin/jurisdictions/tax/admins/{tax_admin.id}").status_code == 200

    # It has no legal wording yet: the judge's form says so and the document has no legal sections.
    as_user(iac_admin)  # any judge-side user will do; the judge route needs no admin role
    content = client.get("/api/document-content", params={"jurisdiction": TAX_NAME}).json()
    assert content["has_legal_wording"] is False
    assert content["legal_frameworks"] == []
    document = _next_document_text(tmp_path, TAX_NAME, ["asylum_pre_naba"])
    assert "Issues" not in document
    assert "Legal Framework" not in document
    assert not any(ISSUES_INTRO_START in line or "[No legal" in line for line in document)
    assert "Background text" in document

    # The tax admin adds legal wording: the very next document has it, with its sections.
    as_user(tax_admin)
    assert client.get("/api/admin/jurisdictions").json() == [
        {"id": "tax", "name": TAX_NAME, "has_legal_wording": False, "can_edit": True}
    ]
    assert (
        client.post("/api/admin/jurisdictions/tax/document-content/legal-frameworks", json=FRAMEWORK).status_code == 201
    )
    as_user(iac_admin)
    assert client.get("/api/document-content", params={"jurisdiction": "tax"}).json()["has_legal_wording"] is True
    document = _next_document_text(tmp_path, TAX_NAME, ["vat"])
    assert "Issues" in document
    assert "Legal Framework" in document
    assert "The VAT framework applies." in document
    assert "Was the supply zero-rated?" in document

    # They edit it: the next document uses the new wording, not the old.
    as_user(tax_admin)
    edited = {**FRAMEWORK, "content": "The edited VAT framework applies.", "issues": ["Was it exempt?"]}
    assert (
        client.put("/api/admin/jurisdictions/tax/document-content/legal-frameworks/vat", json=edited).status_code == 200
    )
    document = _next_document_text(tmp_path, TAX_NAME, ["vat"])
    assert "The edited VAT framework applies." in document
    assert "Was it exempt?" in document
    assert "The VAT framework applies." not in document

    # None of it touched IAC: same stored row, same timestamp (so no instance refetched it), IAC document unchanged.
    assert _stored_content(database, "iac") == iac_before
    iac_document = _next_document_text(tmp_path, None, ["asylum_pre_naba"])
    assert "Legal Framework" in iac_document
    assert "The edited VAT framework applies." not in iac_document
    assert "VAT" not in "\n".join(iac_document)

    # The tax admin cannot see or change IAC wording, in any way.
    for response in (
        client.get("/api/admin/jurisdictions/iac/document-content"),
        client.get("/api/admin/document-content"),
        client.put(
            "/api/admin/jurisdictions/iac/document-content/hearing-types/face_to_face",
            json={"id": "face_to_face", "title": "X", "hearing_title": "X", "hearing_description": "X"},
        ),
        client.delete("/api/admin/jurisdictions/iac/document-content/legal-frameworks/asylum_pre_naba"),
        client.post("/api/admin/jurisdictions/iac/document-content/legal-frameworks", json={**FRAMEWORK, "id": "z"}),
    ):
        assert response.status_code == 404
    assert _stored_content(database, "iac") == iac_before

    # Deleting the last legal framework takes the jurisdiction back to having none.
    assert client.delete("/api/admin/jurisdictions/tax/document-content/legal-frameworks/vat").status_code == 204
    document = _next_document_text(tmp_path, TAX_NAME, ["vat"])
    assert "Issues" not in document
    assert "Legal Framework" not in document

    # A system administrator may read both, and sees each one's own wording.
    as_user(sysadmin)
    assert client.get("/api/admin/jurisdictions/iac/document-content").status_code == 200
    assert client.get("/api/admin/jurisdictions/tax/document-content").json()["legal_frameworks"] == []

    # A submission naming a jurisdiction that does not exist is refused, not given IAC wording.
    assert client.get("/api/document-content", params={"jurisdiction": "Nowhere"}).status_code == 404


def test_an_existing_iac_admin_edits_iac_exactly_as_before(api, database, tmp_path):
    """The migration maps existing LegalTextManagers to IAC; the unscoped routes work for them."""
    client, as_user = api
    # An admin as the migration leaves them: a LegalTextManager row mapped to IAC.
    admin = _make_user(database, ["LegalTextManager"], "iac")
    as_user(admin)

    content = client.get("/api/admin/document-content").json()
    assert len(content["legal_frameworks"]) == 17
    assert content["jurisdiction"] == {"id": "iac", "name": IAC_NAME}
    assert content["has_legal_wording"] is True

    framework = next(f for f in content["legal_frameworks"] if f["id"] == "asylum_pre_naba")
    framework = {**framework, "content": "Edited IAC wording.", "issues": ["Edited IAC issue?"]}
    assert client.put("/api/admin/document-content/legal-frameworks/asylum_pre_naba", json=framework).status_code == 200

    document = _next_document_text(tmp_path, None, ["asylum_pre_naba"])
    assert "Edited IAC wording." in document
    assert "Edited IAC issue?" in document
    # The same through the new scoped route.
    scoped = client.get("/api/admin/jurisdictions/iac/document-content").json()
    assert (
        next(f for f in scoped["legal_frameworks"] if f["id"] == "asylum_pre_naba")["content"] == "Edited IAC wording."
    )


def test_a_second_instance_sees_an_admin_save_on_its_next_read(database):
    """Cache invalidation by updated_at works per jurisdiction: another process's cache is refreshed."""
    from transcribe_api.documents import content_loader
    from transcribe_api.domain.jurisdictions import create_jurisdiction

    create_jurisdiction("tax", TAX_NAME)
    assert content_loader.get_document_content("tax")["legal_frameworks"] == []
    iac = content_loader.get_document_content("iac")

    # Another instance saves (this process's cache is not cleared, as it would not be).
    other_instance_wording = {**content_loader.get_document_content("tax"), "legal_frameworks": [FRAMEWORK]}
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE document_content SET content = CAST(:c AS jsonb), updated_at = now() WHERE jurisdiction_id = 'tax'"
            ),
            {"c": json.dumps(other_instance_wording)},
        )

    assert content_loader.get_document_content("tax")["legal_frameworks"] == [FRAMEWORK]
    assert content_loader.get_document_content("iac") == iac


def test_deleting_a_user_removes_their_mapping_and_a_jurisdiction_needs_unique_names(database):
    from transcribe_api.domain.jurisdictions import (
        JurisdictionExistsError,
        create_jurisdiction,
        list_jurisdiction_admins,
    )

    admin = _make_user(database, ["LegalTextManager"], "iac")
    assert [u.id for u in list_jurisdiction_admins("iac")] == [admin.id]
    with database.begin() as connection:
        connection.execute(text('DELETE FROM "user" WHERE id = :i'), {"i": admin.id})
    assert list_jurisdiction_admins("iac") == []

    create_jurisdiction("tax", TAX_NAME)
    with pytest.raises(JurisdictionExistsError):
        create_jurisdiction("tax", "Another name")
    with pytest.raises(JurisdictionExistsError):
        create_jurisdiction("tax2", TAX_NAME)
