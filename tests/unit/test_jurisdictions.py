"""DIAAT-330: each user works in their own jurisdiction, with its wording.

Runs the real lookups against an in-memory SQLite database (JSONB rendered as JSON),
so membership, the choice of jurisdiction and first sign-in are tested with real SQL.
"""

from __future__ import annotations

from datetime import UTC
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import add_jurisdiction, add_member, add_user, client_as, sign_up
from transcribe_api.api.routes_dictation import TEMPLATE_NOT_AVAILABLE
from transcribe_api.domain import interface_dictation, jurisdictions
from transcribe_api.domain.exceptions import JurisdictionConflictError
from transcribe_api.domain.jurisdictions import (
    CHOICE_REQUIRED,
    NO_JURISDICTION,
    NOT_AVAILABLE,
    JurisdictionSettings,
    choose,
    default_jurisdiction_id,
    load_memberships,
    parse_settings,
    resolve,
)
from transcribe_api.domain.models_dictation import (
    Jurisdiction,
    JurisdictionMembership,
    Transcription,
    User,
)
from transcribe_api.runtime.telemetry import transcription_id_from_key

pytestmark = pytest.mark.real_jurisdictions

ET_WORDING = {"records.column_title": "Claim", "home.intro": "Employment Tribunal dictation."}


@pytest.fixture
def engine(jurisdiction_db):
    return jurisdiction_db


def _memberships(engine, user: User) -> list[tuple[str, str]]:
    with Session(engine) as session:
        rows = session.exec(
            select(Jurisdiction.code, JurisdictionMembership.role)
            .join(JurisdictionMembership, JurisdictionMembership.jurisdiction_id == Jurisdiction.id)
            .where(JurisdictionMembership.user_id == user.id)
        ).all()
    return sorted(tuple(r) for r in rows)


# Settings ---------------------------------------------------------------------------


def test_settings_accept_known_wording_keys_only():
    JurisdictionSettings.model_validate({"wording": {"records.column_title": "Claim"}})
    with pytest.raises(ValueError, match="unknown wording keys: nope"):
        JurisdictionSettings.model_validate({"wording": {"nope": "x"}})
    with pytest.raises(ValueError, match="longer than"):
        JurisdictionSettings.model_validate({"wording": {"home.intro": "x" * 501}})
    with pytest.raises(ValueError):
        JurisdictionSettings.model_validate({"template": "et_judgment"})


def test_a_stored_row_keeps_what_this_release_knows_and_drops_the_rest(caplog):
    """A key from a newer release or a hand edit must not cost the IAC its template."""
    settings = parse_settings(
        {
            "wording": {"records.column_title": "Hearing", "future.key": "x", "home.intro": 3},
            "template": "iac_decision",
            "features": {"hearing_recordings": True, "future_feature": True},
            "future": {},
        }
    )
    assert settings.wording == {"records.column_title": "Hearing"}
    assert settings.template == "iac_decision"
    assert settings.features.hearing_recordings is True
    assert "ignored" in caplog.text


def test_a_stored_row_with_an_unknown_template_has_none():
    settings = parse_settings({"template": "et_judgment", "features": {"hearing_recordings": "yes"}})
    assert settings.template is None
    assert settings.features.hearing_recordings is False
    assert parse_settings(None) == JurisdictionSettings()


# Which jurisdiction a request works in -----------------------------------------------


def test_memberships_are_the_users_enabled_jurisdictions_only(engine):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET", settings={"wording": ET_WORDING})
    off = add_jurisdiction(engine, "OFF", enabled=False)
    user, other = add_user(engine, roles=()), add_user(engine, roles=())
    add_member(engine, user, iac)
    add_member(engine, user, et, role="admin")
    add_member(engine, user, off)
    add_member(engine, other, iac)

    memberships = load_memberships(user.id)

    assert [(m.code, m.role) for m in memberships] == [("ET", "admin"), ("IAC", "judge")]
    assert memberships[0].settings.wording == ET_WORDING


def _error_code(excinfo) -> tuple[int, str]:
    return excinfo.value.status_code, excinfo.value.detail["code"]


def test_a_user_with_no_jurisdiction_is_refused(engine):
    user = add_user(engine, roles=())
    with pytest.raises(HTTPException) as excinfo:
        resolve(user.id, None, None)
    assert _error_code(excinfo) == (403, NO_JURISDICTION)


def test_a_user_with_one_jurisdiction_works_in_it(engine):
    iac = add_jurisdiction(engine, "IAC")
    user = add_user(engine, roles=())
    add_member(engine, user, iac)
    assert resolve(user.id, None, None).code == "IAC"


def test_a_user_with_several_must_choose_unless_they_have(engine):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine, roles=())
    add_member(engine, user, iac)
    add_member(engine, user, et)

    with pytest.raises(HTTPException) as excinfo:
        resolve(user.id, None, None)
    assert _error_code(excinfo) == (409, CHOICE_REQUIRED)
    assert resolve(user.id, et.id, None).code == "ET"


def test_the_header_names_the_jurisdiction_and_must_be_one_of_the_users(engine):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine, roles=())
    add_member(engine, user, iac)
    add_member(engine, user, et)

    assert resolve(user.id, iac.id, " et ").code == "ET"  # the header wins over the remembered choice
    with pytest.raises(HTTPException) as excinfo:
        resolve(user.id, iac.id, "SSCS")
    assert _error_code(excinfo) == (403, NOT_AVAILABLE)


def test_a_remembered_choice_the_user_no_longer_has_is_ignored(engine):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine, roles=())
    add_member(engine, user, iac)
    assert resolve(user.id, et.id, None).code == "IAC"  # removed from the ET: their only one


def test_choose_needs_no_database():
    with pytest.raises(HTTPException) as excinfo:
        choose((), None, None)
    assert _error_code(excinfo) == (403, NO_JURISDICTION)


def test_work_without_a_request_goes_in_the_users_remembered_or_only_jurisdiction(engine):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    single, several, none = add_user(engine, roles=()), add_user(engine, roles=()), add_user(engine, roles=())
    add_member(engine, single, iac)
    add_member(engine, several, iac)
    add_member(engine, several, et)

    assert default_jurisdiction_id(single.id) == iac.id
    assert default_jurisdiction_id(several.id) is None  # no choice: in no jurisdiction
    assert default_jurisdiction_id(none.id) is None
    with Session(engine) as session:
        row = session.get(User, several.id)
        row.active_jurisdiction_id = et.id
        session.add(row)
        session.commit()
    assert default_jurisdiction_id(several.id) == et.id


# First sign-in ----------------------------------------------------------------------


def test_a_new_user_claims_memberships_added_for_their_email(engine):
    add_jurisdiction(engine, "IAC", auto_enrol=True)
    et = add_jurisdiction(engine, "ET")
    add_member(engine, None, et, role="admin", email="et.judge@example.test")

    user = sign_up(engine, "ET.Judge@Example.test")

    # Claimed, and not also put in the IAC: an ET test judge sees the ET only.
    assert _memberships(engine, user) == [("ET", "admin")]
    with Session(engine) as session:
        assert session.exec(select(JurisdictionMembership.email)).all() == [None]


def test_a_new_user_with_nothing_pending_joins_the_auto_enrol_jurisdictions(engine):
    add_jurisdiction(engine, "IAC", auto_enrol=True)
    add_jurisdiction(engine, "ET")
    add_jurisdiction(engine, "OLD", auto_enrol=True, enabled=False)

    user = sign_up(engine, "new.judge@example.test")

    assert _memberships(engine, user) == [("IAC", "judge")]


def test_without_an_auto_enrol_jurisdiction_a_new_user_has_none(engine):
    add_jurisdiction(engine, "ET")
    assert _memberships(engine, sign_up(engine, "someone@example.test")) == []


# Records are made in, and listed from, one jurisdiction ---------------------------------


def test_a_new_transcription_is_made_in_the_requests_jurisdiction_and_keeps_it(engine):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine, roles=())

    saved = interface_dictation.save_transcription(Transcription(title="A v B"), user.id, iac.id)
    assert saved.jurisdiction_id == iac.id

    # A later save naming another jurisdiction does not move it...
    resaved = interface_dictation.save_transcription(
        Transcription(id=saved.id, title="renamed", jurisdiction_id=et.id), user.id, iac.id
    )
    assert resaved.jurisdiction_id == iac.id
    # ...and one made from another jurisdiction is refused (DIAAT-334).
    with pytest.raises(JurisdictionConflictError):
        interface_dictation.save_transcription(Transcription(id=saved.id, title="moved"), user.id, et.id)


def test_a_client_cannot_choose_a_new_transcriptions_jurisdiction(engine):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine, roles=())
    saved = interface_dictation.save_transcription(Transcription(title="x", jurisdiction_id=et.id), user.id, iac.id)
    assert saved.jurisdiction_id == iac.id


def test_the_list_shows_the_current_jurisdictions_transcriptions_only(engine):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine, roles=())
    interface_dictation.save_transcription(Transcription(title="IAC hearing"), user.id, iac.id)
    interface_dictation.save_transcription(Transcription(title="ET claim"), user.id, et.id)

    def titles(jurisdiction):
        return [t.title for t in interface_dictation.fetch_transcriptions_metadata(user.id, UTC, jurisdiction.id)]

    assert titles(iac) == ["IAC hearing"]
    assert titles(et) == ["ET claim"]


# GET /api/jurisdictions/me ------------------------------------------------------------


def test_me_gives_the_current_jurisdiction_with_its_wording(engine, jurisdiction_app):
    iac = add_jurisdiction(
        engine,
        "IAC",
        settings={"wording": {"records.column_title": "Hearing"}, "template": "iac_decision",
                  "features": {"hearing_recordings": True}},
    )
    user = add_user(engine, roles=())
    add_member(engine, user, iac)

    response = client_as(jurisdiction_app, engine, user).get("/api/jurisdictions/me")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "ready",
        "current": {
            "code": "IAC",
            "name": "IAC name",
            "role": "judge",
            "template": "iac_decision",
            "features": {"hearing_recordings": True},
            "wording": {"records.column_title": "Hearing"},
        },
        "jurisdictions": [{"code": "IAC", "name": "IAC name", "role": "judge"}],
    }


def test_me_says_when_the_user_has_no_jurisdiction(engine, jurisdiction_app):
    response = client_as(jurisdiction_app, engine, add_user(engine, roles=())).get("/api/jurisdictions/me")
    assert response.json() == {"status": "no_jurisdiction", "current": None, "jurisdictions": []}


def test_me_asks_a_user_with_several_to_choose(engine, jurisdiction_app):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine, roles=())
    add_member(engine, user, iac)
    add_member(engine, user, et)

    body = client_as(jurisdiction_app, engine, user).get("/api/jurisdictions/me").json()

    assert body["status"] == "choose"
    assert body["current"] is None
    assert [j["code"] for j in body["jurisdictions"]] == ["ET", "IAC"]


def test_me_follows_the_tabs_header_and_ignores_one_the_user_no_longer_has(engine, jurisdiction_app):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET", settings={"wording": ET_WORDING})
    user = add_user(engine, roles=())
    add_member(engine, user, iac)
    add_member(engine, user, et)
    tab = client_as(jurisdiction_app, engine, user)

    et_tab = tab.get("/api/jurisdictions/me", headers={"X-Jurisdiction": "ET"}).json()
    assert et_tab["current"]["code"] == "ET"
    assert et_tab["current"]["wording"] == ET_WORDING
    assert tab.get("/api/jurisdictions/me", headers={"X-Jurisdiction": "SSCS"}).json()["status"] == "choose"


# Routes work in the request's jurisdiction ----------------------------------------------


def test_the_list_route_lists_the_jurisdiction_named_by_the_header(engine, jurisdiction_app, mocker):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine)
    add_member(engine, user, iac)
    add_member(engine, user, et)
    fetch = mocker.patch("transcribe_api.api.routes_dictation.fetch_transcriptions_metadata", return_value=[])
    client = client_as(jurisdiction_app, engine, user)

    assert client.get("/api/transcriptions-metadata", headers={"X-Jurisdiction": "ET"}).status_code == 200
    assert fetch.call_args.args[2] == et.id
    # No header and no choice: the user is asked to choose, nothing is listed.
    response = client.get("/api/transcriptions-metadata")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == CHOICE_REQUIRED
    assert fetch.call_count == 1


def test_routes_refuse_a_user_with_no_jurisdiction(engine, jurisdiction_app, mocker):
    fetch = mocker.patch("transcribe_api.api.routes_dictation.fetch_transcriptions_metadata", return_value=[])
    response = client_as(jurisdiction_app, engine, add_user(engine)).get("/api/transcriptions-metadata")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == NO_JURISDICTION
    fetch.assert_not_called()


def test_document_content_is_the_iac_templates_only(engine, jurisdiction_app, mocker):
    iac = add_jurisdiction(engine, "IAC", settings={"template": "iac_decision"})
    et = add_jurisdiction(engine, "ET")
    user = add_user(engine)
    add_member(engine, user, iac)
    add_member(engine, user, et)
    mocker.patch(
        "transcribe_api.api.routes_dictation.get_document_content",
        return_value={"hearing_types": [], "legal_frameworks": [], "anonymity": {}},
    )
    client = client_as(jurisdiction_app, engine, user)

    assert client.get("/api/document-content", headers={"X-Jurisdiction": "IAC"}).status_code == 200
    refused = client.get("/api/document-content", headers={"X-Jurisdiction": "ET"})
    assert (refused.status_code, refused.json()) == (404, {"detail": TEMPLATE_NOT_AVAILABLE})


def test_the_live_draft_is_the_jurisdictions_own(engine, jurisdiction_app, mocker):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine)
    add_member(engine, user, iac)
    add_member(engine, user, et)
    upsert = mocker.patch("transcribe_api.api.routes_dictation.upsert_live_draft")
    get = mocker.patch("transcribe_api.api.routes_dictation.get_live_draft", return_value=None)
    client = client_as(jurisdiction_app, engine, user)
    body = {"transcript": {"general": []}, "form_data": {}}

    assert client.put("/api/live-draft", json=body, headers={"X-Jurisdiction": "ET"}).status_code == 204
    assert upsert.call_args.args[:2] == (user.id, et.id)
    assert client.get("/api/live-draft", headers={"X-Jurisdiction": "IAC"}).status_code == 404
    assert get.call_args.args == (user.id, iac.id)


def test_first_login_through_the_resolver_enrols_the_user(engine, monkeypatch):
    from transcribe_api.domain.auth import dependencies_dictation

    add_jurisdiction(engine, "IAC", auto_enrol=True)
    monkeypatch.setattr(dependencies_dictation, "get_engine", lambda: engine)

    resolved = dependencies_dictation._resolve_user(str(uuid4()), "first.time@example.test", ["Judge"])

    assert _memberships(engine, resolved.db_user) == [("IAC", "judge")]


# An upload's transcription is made in the tab's jurisdiction ---------------------------


def test_an_upload_is_transcribed_in_the_jurisdiction_it_was_started_in(engine, jurisdiction_app, mocker):
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine)
    add_member(engine, user, iac)
    add_member(engine, user, et)
    mocker.patch("transcribe_api.api.routes_dictation.get_settings").return_value.DIRECT_SDK_ACCESS = False

    response = client_as(jurisdiction_app, engine, user).post(
        "/api/get-upload-url", json={"file_extension": "mp3"}, headers={"X-Jurisdiction": "ET"}
    )

    assert response.status_code == 200
    upload_id = transcription_id_from_key(response.json()["user_upload_s3_file_key"])
    with Session(engine) as session:
        # No transcription yet (nothing to list until the upload arrives), only where it started.
        assert session.exec(select(Transcription)).all() == []
    # The poller, later, with no request: the transcription is made in the ET.
    made = interface_dictation.load_or_create_transcription(
        upload_id, user.id, lambda: jurisdictions.upload_jurisdiction_id(upload_id, user.id)
    )
    assert made.jurisdiction_id == et.id


def test_an_upload_from_before_jurisdictions_goes_in_the_users_only_jurisdiction(engine):
    iac = add_jurisdiction(engine, "IAC")
    user = add_user(engine, roles=())
    add_member(engine, user, iac)
    assert jurisdictions.upload_jurisdiction_id(uuid4(), user.id) == iac.id
    assert jurisdictions.upload_jurisdiction_id(None, user.id) == iac.id


def test_an_upload_started_in_a_jurisdiction_the_user_has_since_left_stays_there(engine):
    """Not moved to the jurisdiction they still have, where its admins could read it:
    the ET's material stays the ET's, out of the user's reach until they are added back."""
    iac, et = add_jurisdiction(engine, "IAC"), add_jurisdiction(engine, "ET")
    user = add_user(engine, roles=())
    add_member(engine, user, iac)  # and no longer the ET
    upload_id = uuid4()
    jurisdictions.record_upload_jurisdiction(upload_id, et.id)
    assert jurisdictions.upload_jurisdiction_id(upload_id, user.id) == et.id

    made = interface_dictation.load_or_create_transcription(
        upload_id, user.id, lambda: jurisdictions.upload_jurisdiction_id(upload_id, user.id)
    )
    assert made.jurisdiction_id == et.id
    assert jurisdictions.owned_transcription_jurisdiction(made.id, user.id) == (True, et.id)


def test_the_jurisdiction_of_an_existing_transcription_is_not_looked_up(engine):
    iac = add_jurisdiction(engine, "IAC")
    user = add_user(engine, roles=())
    saved = interface_dictation.save_transcription(Transcription(title="x"), user.id, iac.id)

    def must_not_ask():
        raise AssertionError("looked up")

    assert interface_dictation.load_or_create_transcription(saved.id, user.id, must_not_ask).id == saved.id


def test_only_a_jurisdiction_with_the_decision_template_can_submit_a_decision(engine, jurisdiction_app, mocker):
    et = add_jurisdiction(engine, "ET")
    user = add_user(engine)
    add_member(engine, user, et)
    save = mocker.patch("transcribe_api.api.routes_dictation.save_transcription")
    render = mocker.patch("transcribe_api.api.routes_dictation.render_hearing_document")
    body = {
        "form_data": {
            "location": "Birmingham",
            "hearingDate": "2026-01-15",
            "judgeName": "Judge",
            "appellantName": "A",
            "respondent": "B",
            "hearingType": "substantive",
            "appealableDecisionDate": "2025-06-01",
            "documentType": "determination",
        },
        "messages": {"background": [], "evidence": [], "facts": []},
    }

    response = client_as(jurisdiction_app, engine, user).post("/api/live-transcription/submission", json=body)

    # The template gate, not an unknown route, and before anything is made.
    assert (response.status_code, response.json()) == (404, {"detail": TEMPLATE_NOT_AVAILABLE})
    render.assert_not_called()
    save.assert_not_called()


def test_a_cross_origin_page_may_send_the_jurisdiction_header(monkeypatch):
    """Local development calls the API from another origin: the preflight must allow it."""
    from transcribe_api.api.app import create_app

    monkeypatch.setenv("ENVIRONMENT", "local")
    client = TestClient(create_app())
    response = client.options(
        "/api/jurisdictions/me",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-jurisdiction",
        },
    )
    assert response.status_code == 200
    assert "x-jurisdiction" in response.headers["access-control-allow-headers"].lower()
