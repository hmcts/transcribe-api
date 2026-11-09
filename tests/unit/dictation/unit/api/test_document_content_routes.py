"""GET /api/document-content: the active jurisdiction's document content, for a Judge (document_content.read).

(This file was skipped as a whole before DIAAT-376: it named an app module and a dependency that no longer
exist. Its one test is real now; the per-jurisdiction behaviour is tests/unit/test_jurisdiction_legal_wording.py.)
"""

import copy
import json
from pathlib import Path

import pytest

from tests.unit.dictation.unit.api.test_routes import _make_user

BUNDLED = json.loads(
    (Path(__file__).parents[5] / "src" / "transcribe_api" / "data" / "document_content.json").read_text()
)


@pytest.fixture
def content(mocker):
    return mocker.patch(
        "transcribe_api.api.routes_dictation.get_document_content", return_value=copy.deepcopy(BUNDLED)
    )


def test_get_document_content_route_returns_the_content_uncached(client, as_user, content) -> None:
    as_user(_make_user(roles=["Judge"]))

    response = client.get("/api/document-content")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["hearing_types"][0]["id"] == "face_to_face"
    assert any(item["id"] == "eea_deportation" for item in body["legal_frameworks"])
    assert [status["id"] for status in body["anonymity"]["statuses"]] == ["granted", "sought_but_refused", "not_sought"]
    assert body["has_legal_wording"] is True


@pytest.mark.parametrize("role", ["JurisdictionAdministrator", "JurisdictionSuperAdministrator"])
def test_the_roles_without_document_content_read_are_refused(client, as_user, content, role) -> None:
    as_user(_make_user(roles=[role]))

    assert client.get("/api/document-content").status_code == 403
    content.assert_not_called()


def test_a_system_administrator_with_no_context_is_refused(client, as_user, content) -> None:
    from transcribe_api.domain.auth.auth_models_dictation import AuthenticatedUser

    as_user(AuthenticatedUser(db_user=_make_user(), ctx=None, sysadmin=True))

    assert client.get("/api/document-content").status_code == 403
    content.assert_not_called()
