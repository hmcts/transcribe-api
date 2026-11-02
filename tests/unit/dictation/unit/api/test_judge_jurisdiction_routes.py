"""What a judge's client learns about a jurisdiction's wording, and what a submission may name (DIAAT-342)."""

import uuid
from unittest.mock import MagicMock

import pytest

from transcribe_api.domain.models_dictation import Jurisdiction, User

ROUTES = "transcribe_api.api.routes_dictation"
IAC = Jurisdiction(id="iac", name="First-tier Tribunal Immigration and Asylum Chamber")
TAX = Jurisdiction(id="tax", name="Tax Tribunal")

IAC_CONTENT = {
    "hearing_types": [
        {"id": "face_to_face", "title": "Face to face", "hearing_title": "H", "hearing_description": "D"}
    ],
    "legal_frameworks": [{"id": "fw", "title": "FW", "rank": 1, "content": "C", "issues": ["Q"]}],
    "anonymity": {"statuses": []},
}
TAX_CONTENT = {"hearing_types": IAC_CONTENT["hearing_types"], "legal_frameworks": [], "anonymity": {"statuses": []}}


def _judge() -> User:
    user = User(id=uuid.uuid4(), email="judge@justice.gov.uk", azure_user_id=str(uuid.uuid4()))
    user.__dict__["app_roles"] = ["Judge"]
    user.has_completed_onboarding = True
    return user


@pytest.fixture
def jurisdictions(mocker):
    by_key = {j.id: j for j in (IAC, TAX)} | {j.name.lower(): j for j in (IAC, TAX)}
    content = {"iac": IAC_CONTENT, "tax": TAX_CONTENT}
    mocker.patch(
        f"{ROUTES}.resolve_jurisdiction",
        side_effect=lambda v: by_key.get((v or IAC.name).strip().lower()),
    )
    mocker.patch(f"{ROUTES}.get_document_content", side_effect=lambda jurisdiction_id: content[jurisdiction_id])
    mocker.patch(f"{ROUTES}.list_jurisdictions", return_value=[IAC, TAX])
    mocker.patch(
        f"{ROUTES}.has_legal_wording",
        side_effect=lambda jurisdiction_id: bool(content[jurisdiction_id]["legal_frameworks"]),
    )


def test_without_a_jurisdiction_the_judge_gets_iac_wording_as_before(client, as_user, jurisdictions):
    as_user(_judge())

    response = client.get("/api/document-content")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["jurisdiction"] == {"id": "iac", "name": IAC.name}
    assert body["has_legal_wording"] is True
    assert body["legal_frameworks"][0]["id"] == "fw"
    assert body["hearing_types"][0]["id"] == "face_to_face"


@pytest.mark.parametrize("named", ["tax", "Tax Tribunal", " tax tribunal "])
def test_a_jurisdiction_is_named_by_id_or_display_name(client, as_user, jurisdictions, named):
    as_user(_judge())

    body = client.get("/api/document-content", params={"jurisdiction": named}).json()

    assert body["jurisdiction"] == {"id": "tax", "name": "Tax Tribunal"}


def test_a_jurisdiction_without_legal_wording_returns_no_legal_frameworks(client, as_user, jurisdictions):
    as_user(_judge())

    body = client.get("/api/document-content", params={"jurisdiction": "tax"}).json()

    assert body["has_legal_wording"] is False
    assert body["legal_frameworks"] == []
    assert body["hearing_types"][0]["id"] == "face_to_face"


def test_an_unknown_jurisdiction_is_404_not_iac_wording(client, as_user, jurisdictions):
    as_user(_judge())

    response = client.get("/api/document-content", params={"jurisdiction": "Nowhere Tribunal"})

    assert response.status_code == 404
    assert "legal_frameworks" not in response.text


def test_the_jurisdictions_list_says_which_have_legal_wording(client, as_user, jurisdictions):
    as_user(_judge())

    response = client.get("/api/jurisdictions")

    assert response.status_code == 200
    assert response.json() == [
        {"id": "iac", "name": IAC.name, "has_legal_wording": True},
        {"id": "tax", "name": "Tax Tribunal", "has_legal_wording": False},
    ]


_SUBMISSION = {
    "form_data": {
        "location": "Taylor House",
        "hearingDate": "2026-01-15",
        "judgeName": "J",
        "appellantName": "A",
        "respondent": "R",
        "hearingType": "face_to_face",
        "appealableDecisionDate": "2025-06-01",
        "documentType": "Decision",
    },
    "messages": {"background": [], "evidence": [], "facts": []},
}


def test_a_submission_naming_an_unknown_jurisdiction_is_refused_before_anything_is_stored(
    client, as_user, jurisdictions, mocker
):
    as_user(_judge())
    blob_manager = mocker.patch(f"{ROUTES}.AsyncAzureBlobManager", return_value=MagicMock())
    render = mocker.patch(f"{ROUTES}.render_hearing_document")
    body = {**_SUBMISSION, "form_data": {**_SUBMISSION["form_data"], "jurisdiction": "Nowhere Tribunal"}}

    response = client.post("/api/live-transcription/submission", json=body)

    assert response.status_code == 422
    blob_manager.assert_not_called()
    render.assert_not_called()
