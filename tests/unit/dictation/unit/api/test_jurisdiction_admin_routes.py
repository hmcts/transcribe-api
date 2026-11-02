"""Route-level tests for the jurisdiction-scoped admin wording routes (DIAAT-342).

The database is replaced by an in-memory world (jurisdictions, who administers
them, their wording), so these tests exercise the real authorisation and the real
route handlers. The same flows run against a real Postgres in
tests/unit/test_jurisdiction_wording_postgres.py.
"""

import copy
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transcribe_api.domain.jurisdictions import JurisdictionExistsError
from transcribe_api.domain.models_dictation import Jurisdiction, User

ROUTES = "transcribe_api.api.routes_dictation_privileged"
IAC = "iac"
OTHER = "employment"


def _wording(marker: str, *, frameworks: bool = True) -> dict:
    return {
        "hearing_types": [
            {"id": "face_to_face", "title": "Face to face", "hearing_title": marker, "hearing_description": marker}
        ],
        "legal_frameworks": (
            [{"id": "fw", "title": marker, "rank": 1, "content": f"{marker} framework", "issues": [f"{marker} issue"]}]
            if frameworks
            else []
        ),
        "anonymity": {"statuses": []},
    }


def _user(roles: list[str], email: str = "admin@justice.gov.uk") -> User:
    user = User(id=uuid.uuid4(), email=email, azure_user_id=f"oid-{uuid.uuid4()}", role=roles[0] if roles else "Normal")
    user.__dict__["app_roles"] = roles
    return user


class World:
    """What the database holds: jurisdictions, their wording, and who administers them."""

    def __init__(self) -> None:
        self.jurisdictions = {
            IAC: Jurisdiction(id=IAC, name="First-tier Tribunal Immigration and Asylum Chamber"),
            OTHER: Jurisdiction(id=OTHER, name="Employment Tribunal"),
        }
        self.wording = {IAC: _wording("IAC"), OTHER: _wording("EMPLOYMENT")}
        self.admins: dict[str, set[uuid.UUID]] = {IAC: set(), OTHER: set()}
        self.users: dict[uuid.UUID, User] = {}
        self.audit: list = []
        self.saves: list[str] = []

    def add_user(self, user: User, *jurisdictions: str) -> User:
        self.users[user.id] = user
        for j in jurisdictions:
            self.admins[j].add(user.id)
        return user

    # The functions the routes import, over the world.
    def get_jurisdiction(self, jurisdiction_id):
        return self.jurisdictions.get(jurisdiction_id)

    def list_jurisdictions(self):
        return sorted(self.jurisdictions.values(), key=lambda j: j.name)

    def get_admin_jurisdiction_ids(self, user_id):
        return {j for j, users in self.admins.items() if user_id in users}

    def load(self, jurisdiction_id):
        return self.wording[jurisdiction_id]

    def save(self, jurisdiction_id, data):
        self.saves.append(jurisdiction_id)
        self.wording[jurisdiction_id] = copy.deepcopy(data)

    def has_legal_wording(self, jurisdiction_id):
        return bool(self.wording[jurisdiction_id].get("legal_frameworks"))

    def create(self, jurisdiction_id, name):
        # The real create_jurisdiction makes this check atomically (tests/unit/test_jurisdiction_wording_postgres.py).
        wanted = {jurisdiction_id.lower(), name.lower()}
        if any(wanted & {j.id.lower(), j.name.lower()} for j in self.jurisdictions.values()):
            raise JurisdictionExistsError(jurisdiction_id)
        self.jurisdictions[jurisdiction_id] = Jurisdiction(id=jurisdiction_id, name=name)
        self.wording[jurisdiction_id] = _wording("NEW", frameworks=False)
        self.admins[jurisdiction_id] = set()
        return self.jurisdictions[jurisdiction_id]

    def list_admins(self, jurisdiction_id):
        return [self.users[u] for u in self.admins[jurisdiction_id]]

    def add_admin(self, jurisdiction_id, user_id):
        added = user_id not in self.admins[jurisdiction_id]
        self.admins[jurisdiction_id].add(user_id)
        return added

    def remove_admin(self, jurisdiction_id, user_id):
        present = user_id in self.admins[jurisdiction_id]
        self.admins[jurisdiction_id].discard(user_id)
        return present

    def get_user(self, user_id):
        return self.users[user_id]


@pytest.fixture
def world(mocker, app):  # app first: it must be the one to import the router
    w = World()
    for name, target in {
        "get_jurisdiction": w.get_jurisdiction,
        "list_jurisdictions": w.list_jurisdictions,
        "get_admin_jurisdiction_ids": w.get_admin_jurisdiction_ids,
        "load_document_content": w.load,
        "save_document_content": w.save,
        "has_legal_wording": w.has_legal_wording,
        "create_jurisdiction": w.create,
        "list_jurisdiction_admins": w.list_admins,
        "add_jurisdiction_admin": w.add_admin,
        "remove_jurisdiction_admin": w.remove_admin,
        "get_user_by_id": w.get_user,
    }.items():
        mocker.patch(f"{ROUTES}.{name}", side_effect=target)
    mocker.patch(f"{ROUTES}.write_audit_event", side_effect=w.audit.append)
    return w


@pytest.fixture
def app():
    # test_privileged_routes imports the router on first use, after test_live_transcription_submission_logging
    # has reloaded the auth module. A router imported before that reload holds a stale get_current_user that
    # tests cannot override, so leave the module as found: not imported, if it was not.
    import importlib
    import sys

    import transcribe_api.api as api_package

    name = "transcribe_api.api.routes_dictation_privileged"
    was_imported = name in sys.modules
    router = importlib.import_module(name).router

    test_app = FastAPI()
    test_app.include_router(router, prefix="/api")
    yield test_app
    if not was_imported:
        sys.modules.pop(name, None)
        if hasattr(api_package, "routes_dictation_privileged"):
            delattr(api_package, "routes_dictation_privileged")


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def as_user(app):
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    yield lambda user: app.dependency_overrides.__setitem__(get_current_user, lambda: user)
    app.dependency_overrides.pop(get_current_user, None)


def _scoped(jurisdiction_id: str, path: str = "") -> str:
    return f"/api/admin/jurisdictions/{jurisdiction_id}/document-content{path}"


HEARING_PAYLOAD = {"id": "face_to_face", "title": "T", "hearing_title": "Changed", "hearing_description": "Changed."}
FRAMEWORK_PAYLOAD = {"id": "fw", "title": "T", "rank": 1, "content": "Changed.", "issues": []}
NEW_FRAMEWORK_PAYLOAD = {"id": "fw2", "title": "T2", "rank": 2, "content": "New.", "issues": ["q"]}
ANONYMITY_PAYLOAD = {
    "id": "granted",
    "title": "T",
    "content": "C",
    "identifier": "",
    "header": "H",
    "order_header": "OH",
    "order_text": "OT",
}

# Every route that reads or writes wording: (method, path suffix, json body)
WRITE_ROUTES = [
    ("put", "/hearing-types/face_to_face", HEARING_PAYLOAD),
    ("post", "/hearing-types", {**HEARING_PAYLOAD, "id": "new_type"}),
    ("delete", "/hearing-types/face_to_face", None),
    ("put", "/legal-frameworks/fw", FRAMEWORK_PAYLOAD),
    ("post", "/legal-frameworks", NEW_FRAMEWORK_PAYLOAD),
    ("delete", "/legal-frameworks/fw", None),
    ("put", "/anonymity-statuses/granted", ANONYMITY_PAYLOAD),
]


# ---------------------------------------------------------------------------
# Acceptance 1: an admin sees only their jurisdiction's wording
# ---------------------------------------------------------------------------


def test_an_admin_lists_only_the_jurisdictions_they_are_mapped_to(client, as_user, world):
    admin = world.add_user(_user(["LegalTextManager"]), OTHER)
    as_user(admin)

    body = client.get("/api/admin/jurisdictions").json()

    assert body == [{"id": OTHER, "name": "Employment Tribunal", "has_legal_wording": True, "can_edit": True}]


def test_an_admin_reads_their_jurisdictions_wording_and_nothing_of_the_others(client, as_user, world):
    admin = world.add_user(_user(["LegalTextManager"]), OTHER)
    as_user(admin)

    response = client.get(_scoped(OTHER))

    assert response.status_code == 200
    body = response.json()
    assert body["jurisdiction"] == {"id": OTHER, "name": "Employment Tribunal"}
    assert body["has_legal_wording"] is True
    assert body["legal_frameworks"][0]["title"] == "EMPLOYMENT"
    assert "IAC" not in response.text


def test_an_admin_with_no_mapping_sees_no_jurisdictions(client, as_user, world):
    as_user(world.add_user(_user(["LegalTextManager"])))

    assert client.get("/api/admin/jurisdictions").json() == []
    assert client.get(_scoped(IAC)).status_code == 404


def test_a_jurisdiction_without_legal_wording_says_so(client, as_user, world):
    world.wording[OTHER] = _wording("EMPLOYMENT", frameworks=False)
    as_user(world.add_user(_user(["LegalTextManager"]), OTHER))

    assert client.get("/api/admin/jurisdictions").json()[0]["has_legal_wording"] is False
    body = client.get(_scoped(OTHER)).json()
    assert body["has_legal_wording"] is False
    assert body["legal_frameworks"] == []


# ---------------------------------------------------------------------------
# Acceptance 3: an admin cannot see or change another jurisdiction's wording
# ---------------------------------------------------------------------------


def test_an_admin_cannot_read_another_jurisdictions_wording(client, as_user, world):
    as_user(world.add_user(_user(["LegalTextManager"]), OTHER))

    response = client.get(_scoped(IAC))

    assert response.status_code == 404
    assert "IAC" not in response.text


@pytest.mark.parametrize(("method", "suffix", "body"), WRITE_ROUTES)
def test_an_admin_cannot_change_another_jurisdictions_wording(client, as_user, world, method, suffix, body):
    as_user(world.add_user(_user(["LegalTextManager"]), OTHER))
    before = copy.deepcopy(world.wording)

    response = getattr(client, method)(_scoped(IAC, suffix), **({"json": body} if body else {}))

    assert response.status_code == 404
    assert world.saves == []
    assert world.wording == before


def test_the_denial_is_the_same_for_a_jurisdiction_that_does_not_exist(client, as_user, world):
    as_user(world.add_user(_user(["LegalTextManager"]), OTHER))

    existing_but_forbidden = client.get(_scoped(IAC))
    missing = client.get(_scoped("no-such-jurisdiction"))

    assert existing_but_forbidden.status_code == missing.status_code == 404
    assert existing_but_forbidden.json() == missing.json()


def test_a_denied_attempt_on_an_existing_jurisdiction_is_audited(client, as_user, world):
    admin = world.add_user(_user(["LegalTextManager"]), OTHER)
    as_user(admin)

    client.put(_scoped(IAC, "/hearing-types/face_to_face"), json=HEARING_PAYLOAD)

    [event] = world.audit
    assert event.resource == f"jurisdiction:{IAC}"
    assert event.user_id == str(admin.id)
    assert event.detail == {"jurisdiction_id": IAC, "write": True}


def test_a_legal_text_manager_cannot_use_the_unscoped_routes_for_another_jurisdiction(client, as_user, world):
    as_user(world.add_user(_user(["LegalTextManager"]), OTHER))

    # The unscoped routes are IAC's; this admin is not mapped to IAC.
    assert client.get("/api/admin/document-content").status_code == 404
    assert client.put("/api/admin/document-content/hearing-types/face_to_face", json=HEARING_PAYLOAD).status_code == 404
    # The optional query parameter picks a jurisdiction but is authorised just the same.
    assert client.get("/api/admin/document-content", params={"jurisdiction_id": OTHER}).status_code == 200


def test_a_judge_cannot_reach_the_admin_routes(client, as_user, world):
    as_user(world.add_user(_user(["Judge"]), OTHER))

    assert client.get("/api/admin/jurisdictions").status_code == 403
    assert client.get(_scoped(OTHER)).status_code == 403
    assert client.put(_scoped(OTHER, "/hearing-types/face_to_face"), json=HEARING_PAYLOAD).status_code == 403


# ---------------------------------------------------------------------------
# Admin allowed: reads and writes in their own jurisdiction, isolated from the others
# ---------------------------------------------------------------------------


def test_an_admin_edits_their_own_jurisdiction_and_only_that_one_changes(client, as_user, world):
    as_user(world.add_user(_user(["LegalTextManager"]), OTHER))
    iac_before = copy.deepcopy(world.wording[IAC])

    assert client.put(_scoped(OTHER, "/hearing-types/face_to_face"), json=HEARING_PAYLOAD).status_code == 200
    assert client.put(_scoped(OTHER, "/legal-frameworks/fw"), json=FRAMEWORK_PAYLOAD).status_code == 200
    assert client.post(_scoped(OTHER, "/legal-frameworks"), json=NEW_FRAMEWORK_PAYLOAD).status_code == 201
    assert client.delete(_scoped(OTHER, "/legal-frameworks/fw2")).status_code == 204

    assert world.saves == [OTHER] * 4
    assert world.wording[OTHER]["hearing_types"][0]["hearing_description"] == "Changed."
    assert world.wording[OTHER]["legal_frameworks"][0]["content"] == "Changed."
    assert world.wording[IAC] == iac_before


def test_a_failed_edit_does_not_alter_the_loaded_wording(client, as_user, world):
    as_user(world.add_user(_user(["LegalTextManager"]), OTHER))
    before = copy.deepcopy(world.wording[OTHER])

    # 409: the framework already exists. Nothing is saved and the source dict is untouched.
    assert (
        client.post(_scoped(OTHER, "/legal-frameworks"), json={**NEW_FRAMEWORK_PAYLOAD, "id": "fw"}).status_code == 409
    )
    assert world.wording[OTHER] == before


def test_deleting_the_last_legal_framework_leaves_no_legal_wording(client, as_user, world):
    as_user(world.add_user(_user(["LegalTextManager"]), OTHER))

    assert client.delete(_scoped(OTHER, "/legal-frameworks/fw")).status_code == 204

    assert client.get(_scoped(OTHER)).json()["has_legal_wording"] is False


def test_the_first_legal_framework_gives_a_jurisdiction_legal_wording(client, as_user, world):
    world.wording[OTHER] = _wording("EMPLOYMENT", frameworks=False)
    as_user(world.add_user(_user(["LegalTextManager"]), OTHER))

    assert client.post(_scoped(OTHER, "/legal-frameworks"), json=NEW_FRAMEWORK_PAYLOAD).status_code == 201

    assert client.get(_scoped(OTHER)).json()["has_legal_wording"] is True


def test_an_admin_of_several_jurisdictions_may_edit_each(client, as_user, world):
    as_user(world.add_user(_user(["LegalTextManager"]), IAC, OTHER))

    assert {j["id"] for j in client.get("/api/admin/jurisdictions").json()} == {IAC, OTHER}
    assert client.put(_scoped(IAC, "/hearing-types/face_to_face"), json=HEARING_PAYLOAD).status_code == 200
    assert client.put(_scoped(OTHER, "/hearing-types/face_to_face"), json=HEARING_PAYLOAD).status_code == 200


# ---------------------------------------------------------------------------
# Acceptance 5: IAC admins editing IAC wording work as before
# ---------------------------------------------------------------------------


def test_an_iac_admin_edits_iac_wording_through_the_old_unscoped_routes(client, as_user, world):
    as_user(world.add_user(_user(["LegalTextManager"]), IAC))

    body = client.get("/api/admin/document-content").json()
    assert body["hearing_types"][0]["id"] == "face_to_face"
    assert body["legal_frameworks"][0]["id"] == "fw"
    assert client.put("/api/admin/document-content/hearing-types/face_to_face", json=HEARING_PAYLOAD).status_code == 200
    assert client.post("/api/admin/document-content/legal-frameworks", json=NEW_FRAMEWORK_PAYLOAD).status_code == 201
    assert client.delete("/api/admin/document-content/legal-frameworks/fw2").status_code == 204

    assert world.saves == [IAC] * 3


# ---------------------------------------------------------------------------
# SystemAdministrator: reads everything, edits only with LegalTextManager too
# ---------------------------------------------------------------------------


def test_a_system_administrator_lists_and_reads_every_jurisdiction_but_cannot_edit(client, as_user, world):
    as_user(world.add_user(_user(["SystemAdministrator"])))

    listed = client.get("/api/admin/jurisdictions").json()
    assert {j["id"]: j["can_edit"] for j in listed} == {IAC: False, OTHER: False}
    assert client.get(_scoped(IAC)).status_code == 200
    assert client.get(_scoped(OTHER)).status_code == 200
    # Writes need the LegalTextManager role, as they always have.
    assert client.put(_scoped(IAC, "/hearing-types/face_to_face"), json=HEARING_PAYLOAD).status_code == 403


def test_a_system_administrator_who_is_also_a_legal_text_manager_edits_any_jurisdiction(client, as_user, world):
    as_user(world.add_user(_user(["SystemAdministrator", "LegalTextManager"])))

    assert all(j["can_edit"] for j in client.get("/api/admin/jurisdictions").json())
    assert client.put(_scoped(OTHER, "/hearing-types/face_to_face"), json=HEARING_PAYLOAD).status_code == 200
    assert client.put(_scoped(IAC, "/hearing-types/face_to_face"), json=HEARING_PAYLOAD).status_code == 200


# ---------------------------------------------------------------------------
# Managing jurisdictions and their admins: SystemAdministrator only
# ---------------------------------------------------------------------------


def test_a_system_administrator_creates_a_jurisdiction_with_no_legal_wording(client, as_user, world):
    as_user(world.add_user(_user(["SystemAdministrator"])))

    response = client.post("/api/admin/jurisdictions", json={"id": "tax", "name": "Tax Tribunal"})

    assert response.status_code == 201
    assert response.json() == {"id": "tax", "name": "Tax Tribunal", "has_legal_wording": False}
    assert "tax" in world.jurisdictions


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "iac", "name": "Other name"},
        {"id": "tax", "name": "Employment Tribunal"},
        {"id": "tax", "name": "iac"},
        {"id": "Tax Tribunal", "name": "Tax"},
        {"id": "", "name": "Tax"},
    ],
)
def test_a_jurisdiction_cannot_reuse_an_id_or_name_or_have_a_malformed_id(client, as_user, world, payload):
    as_user(world.add_user(_user(["SystemAdministrator"])))

    assert client.post("/api/admin/jurisdictions", json=payload).status_code in (409, 422)
    assert set(world.jurisdictions) == {IAC, OTHER}


def test_a_legal_text_manager_cannot_manage_jurisdictions_or_their_admins(client, as_user, world):
    admin = world.add_user(_user(["LegalTextManager"]), OTHER)
    as_user(admin)

    assert client.post("/api/admin/jurisdictions", json={"id": "tax", "name": "Tax"}).status_code == 403
    assert client.get(f"/api/admin/jurisdictions/{OTHER}/admins").status_code == 403
    assert client.put(f"/api/admin/jurisdictions/{IAC}/admins/{admin.id}").status_code == 403
    assert client.delete(f"/api/admin/jurisdictions/{OTHER}/admins/{admin.id}").status_code == 403
    assert world.admins[IAC] == set()


def test_a_system_administrator_maps_and_unmaps_admins(client, as_user, world):
    sysadmin = world.add_user(_user(["SystemAdministrator"]))
    candidate = world.add_user(_user(["LegalTextManager"], email="new.admin@justice.gov.uk"))
    as_user(sysadmin)

    assert client.get(f"/api/admin/jurisdictions/{OTHER}/admins").json() == []
    assert client.put(f"/api/admin/jurisdictions/{OTHER}/admins/{candidate.id}").json() == {
        "user_id": str(candidate.id),
        "email": "new.admin@justice.gov.uk",
        "role": "LegalTextManager",
    }
    assert [a["user_id"] for a in client.get(f"/api/admin/jurisdictions/{OTHER}/admins").json()] == [str(candidate.id)]
    assert [e.event_type for e in world.audit] == ["JURISDICTION_ADMIN_ADDED"]

    # The new admin can now open that jurisdiction, and still not the other one.
    as_user(candidate)
    assert client.get(_scoped(OTHER)).status_code == 200
    assert client.get(_scoped(IAC)).status_code == 404

    as_user(sysadmin)
    assert client.delete(f"/api/admin/jurisdictions/{OTHER}/admins/{candidate.id}").status_code == 204
    assert client.delete(f"/api/admin/jurisdictions/{OTHER}/admins/{candidate.id}").status_code == 404
    assert [e.event_type for e in world.audit][-1] == "JURISDICTION_ADMIN_REMOVED"

    as_user(candidate)
    assert client.get(_scoped(OTHER)).status_code == 404


def test_mapping_an_admin_to_a_missing_jurisdiction_is_404(client, as_user, world):
    sysadmin = world.add_user(_user(["SystemAdministrator"]))
    as_user(sysadmin)

    assert client.put(f"/api/admin/jurisdictions/nope/admins/{sysadmin.id}").status_code == 404
    assert client.get("/api/admin/jurisdictions/nope/admins").status_code == 404
