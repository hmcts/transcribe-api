"""DIAAT-330: each user works in their own jurisdiction, with its wording.

Runs the real lookups against an in-memory SQLite database (JSONB rendered as JSON),
so membership, the choice of jurisdiction and first sign-in are tested with real SQL.
"""

from __future__ import annotations

from datetime import UTC
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from transcribe_api.domain import interface_dictation, jurisdictions
from transcribe_api.runtime.telemetry import transcription_id_from_key
from transcribe_api.domain.jurisdictions import (
    CHOICE_REQUIRED,
    NO_JURISDICTION,
    NOT_AVAILABLE,
    JurisdictionSettings,
    choose,
    default_jurisdiction_id,
    enrol_new_user,
    load_memberships,
    parse_settings,
    resolve,
)
from transcribe_api.domain.models_dictation import (
    Jurisdiction,
    JurisdictionMembership,
    MinuteVersion,
    Tag,
    Transcription,
    TranscriptionJob,
    TranscriptionTagAssociation,
    UploadJurisdiction,
    User,
)

pytestmark = pytest.mark.real_jurisdictions

ET_WORDING = {"records.column_title": "Claim", "home.intro": "Employment Tribunal dictation."}


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):
    return "JSON"


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [
        User.__table__,
        Jurisdiction.__table__,
        JurisdictionMembership.__table__,
        Transcription.__table__,
        TranscriptionJob.__table__,
        MinuteVersion.__table__,
        Tag.__table__,
        TranscriptionTagAssociation.__table__,
        UploadJurisdiction.__table__,
    ]
    SQLModel.metadata.create_all(engine, tables=tables)
    monkeypatch.setattr(jurisdictions, "get_engine", lambda: engine)
    monkeypatch.setattr(interface_dictation, "engine", engine)
    yield engine
    engine.dispose()


def _jurisdiction(engine, code: str, *, enabled=True, auto_enrol=False, settings=None) -> Jurisdiction:
    with Session(engine) as session:
        row = Jurisdiction(
            code=code, name=f"{code} name", enabled=enabled, auto_enrol=auto_enrol, settings=settings or {}
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def _user(engine, email: str | None = None) -> User:
    with Session(engine) as session:
        user = User(email=email or f"{uuid4()}@example.test", azure_user_id=str(uuid4()))
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _member(engine, user: User | None, jurisdiction: Jurisdiction, role="judge", email=None) -> None:
    with Session(engine) as session:
        session.add(
            JurisdictionMembership(
                jurisdiction_id=jurisdiction.id, user_id=user.id if user else None, email=email, role=role
            )
        )
        session.commit()


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
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET", settings={"wording": ET_WORDING})
    off = _jurisdiction(engine, "OFF", enabled=False)
    user, other = _user(engine), _user(engine)
    _member(engine, user, iac)
    _member(engine, user, et, role="admin")
    _member(engine, user, off)
    _member(engine, other, iac)

    memberships = load_memberships(user.id)

    assert [(m.code, m.role) for m in memberships] == [("ET", "admin"), ("IAC", "judge")]
    assert memberships[0].settings.wording == ET_WORDING


def _error_code(excinfo) -> tuple[int, str]:
    return excinfo.value.status_code, excinfo.value.detail["code"]


def test_a_user_with_no_jurisdiction_is_refused(engine):
    user = _user(engine)
    with pytest.raises(HTTPException) as excinfo:
        resolve(user.id, None, None)
    assert _error_code(excinfo) == (403, NO_JURISDICTION)


def test_a_user_with_one_jurisdiction_works_in_it(engine):
    iac = _jurisdiction(engine, "IAC")
    user = _user(engine)
    _member(engine, user, iac)
    assert resolve(user.id, None, None).code == "IAC"


def test_a_user_with_several_must_choose_unless_they_have(engine):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _user(engine)
    _member(engine, user, iac)
    _member(engine, user, et)

    with pytest.raises(HTTPException) as excinfo:
        resolve(user.id, None, None)
    assert _error_code(excinfo) == (409, CHOICE_REQUIRED)
    assert resolve(user.id, et.id, None).code == "ET"


def test_the_header_names_the_jurisdiction_and_must_be_one_of_the_users(engine):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _user(engine)
    _member(engine, user, iac)
    _member(engine, user, et)

    assert resolve(user.id, iac.id, " et ").code == "ET"  # the header wins over the remembered choice
    with pytest.raises(HTTPException) as excinfo:
        resolve(user.id, iac.id, "SSCS")
    assert _error_code(excinfo) == (403, NOT_AVAILABLE)


def test_a_remembered_choice_the_user_no_longer_has_is_ignored(engine):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _user(engine)
    _member(engine, user, iac)
    assert resolve(user.id, et.id, None).code == "IAC"  # removed from the ET: their only one


def test_choose_needs_no_database():
    with pytest.raises(HTTPException) as excinfo:
        choose((), None, None)
    assert _error_code(excinfo) == (403, NO_JURISDICTION)


def test_work_without_a_request_goes_in_the_users_remembered_or_only_jurisdiction(engine):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    single, several, none = _user(engine), _user(engine), _user(engine)
    _member(engine, single, iac)
    _member(engine, several, iac)
    _member(engine, several, et)

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


def _sign_up(engine, email: str) -> User:
    with Session(engine) as session:
        user = User(email=email, azure_user_id=str(uuid4()))
        session.add(user)
        session.flush()
        enrol_new_user(session, user)
        session.commit()
        session.refresh(user)
        return user


def test_a_new_user_claims_memberships_added_for_their_email(engine):
    _jurisdiction(engine, "IAC", auto_enrol=True)
    et = _jurisdiction(engine, "ET")
    _member(engine, None, et, role="admin", email="et.judge@example.test")

    user = _sign_up(engine, "ET.Judge@Example.test")

    # Claimed, and not also put in the IAC: an ET test judge sees the ET only.
    assert _memberships(engine, user) == [("ET", "admin")]
    with Session(engine) as session:
        assert session.exec(select(JurisdictionMembership.email)).all() == [None]


def test_a_new_user_with_nothing_pending_joins_the_auto_enrol_jurisdictions(engine):
    _jurisdiction(engine, "IAC", auto_enrol=True)
    _jurisdiction(engine, "ET")
    _jurisdiction(engine, "OLD", auto_enrol=True, enabled=False)

    user = _sign_up(engine, "new.judge@example.test")

    assert _memberships(engine, user) == [("IAC", "judge")]


def test_without_an_auto_enrol_jurisdiction_a_new_user_has_none(engine):
    _jurisdiction(engine, "ET")
    assert _memberships(engine, _sign_up(engine, "someone@example.test")) == []


# Records are made in, and listed from, one jurisdiction ---------------------------------


def test_a_new_transcription_is_made_in_the_requests_jurisdiction_and_keeps_it(engine):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _user(engine)

    saved = interface_dictation.save_transcription(Transcription(title="A v B"), user.id, iac.id)
    assert saved.jurisdiction_id == iac.id

    # A later save from another jurisdiction, or naming one, does not move it.
    resaved = interface_dictation.save_transcription(
        Transcription(id=saved.id, title="renamed", jurisdiction_id=et.id), user.id, et.id
    )
    assert resaved.jurisdiction_id == iac.id


def test_a_client_cannot_choose_a_new_transcriptions_jurisdiction(engine):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _user(engine)
    saved = interface_dictation.save_transcription(Transcription(title="x", jurisdiction_id=et.id), user.id, iac.id)
    assert saved.jurisdiction_id == iac.id


def test_the_list_shows_the_current_jurisdictions_transcriptions_only(engine):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _user(engine)
    interface_dictation.save_transcription(Transcription(title="IAC hearing"), user.id, iac.id)
    interface_dictation.save_transcription(Transcription(title="ET claim"), user.id, et.id)

    def titles(jurisdiction):
        return [t.title for t in interface_dictation.fetch_transcriptions_metadata(user.id, UTC, jurisdiction.id)]

    assert titles(iac) == ["IAC hearing"]
    assert titles(et) == ["ET claim"]


# GET /api/jurisdictions/me ------------------------------------------------------------


@pytest.fixture
def client():
    from transcribe_api.api.routes_jurisdictions import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


def _as(app, user: User) -> TestClient:
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_me_gives_the_current_jurisdiction_with_its_wording(engine, client):
    iac = _jurisdiction(
        engine,
        "IAC",
        settings={"wording": {"records.column_title": "Hearing"}, "template": "iac_decision",
                  "features": {"hearing_recordings": True}},
    )
    user = _user(engine)
    _member(engine, user, iac)

    response = _as(client, user).get("/api/jurisdictions/me")

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


def test_me_says_when_the_user_has_no_jurisdiction(engine, client):
    response = _as(client, _user(engine)).get("/api/jurisdictions/me")
    assert response.json() == {"status": "no_jurisdiction", "current": None, "jurisdictions": []}


def test_me_asks_a_user_with_several_to_choose(engine, client):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _user(engine)
    _member(engine, user, iac)
    _member(engine, user, et)

    body = _as(client, user).get("/api/jurisdictions/me").json()

    assert body["status"] == "choose"
    assert body["current"] is None
    assert [j["code"] for j in body["jurisdictions"]] == ["ET", "IAC"]


def test_me_follows_the_tabs_header_and_ignores_one_the_user_no_longer_has(engine, client):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET", settings={"wording": ET_WORDING})
    user = _user(engine)
    _member(engine, user, iac)
    _member(engine, user, et)
    tab = _as(client, user)

    et_tab = tab.get("/api/jurisdictions/me", headers={"X-Jurisdiction": "ET"}).json()
    assert et_tab["current"]["code"] == "ET"
    assert et_tab["current"]["wording"] == ET_WORDING
    assert tab.get("/api/jurisdictions/me", headers={"X-Jurisdiction": "SSCS"}).json()["status"] == "choose"


# Routes work in the request's jurisdiction ----------------------------------------------


@pytest.fixture
def dictation_app():
    import transcribe_api.api.routes_dictation

    app = FastAPI()
    app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    return app


def _judge(engine) -> User:
    user = _user(engine)
    user.__dict__["app_roles"] = ["Judge"]
    return user


def test_the_list_route_lists_the_jurisdiction_named_by_the_header(engine, dictation_app, mocker):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _judge(engine)
    _member(engine, user, iac)
    _member(engine, user, et)
    fetch = mocker.patch("transcribe_api.api.routes_dictation.fetch_transcriptions_metadata", return_value=[])
    client = _as(dictation_app, user)

    assert client.get("/api/transcriptions-metadata", headers={"X-Jurisdiction": "ET"}).status_code == 200
    assert fetch.call_args.args[2] == et.id
    # No header and no choice: the user is asked to choose, nothing is listed.
    response = client.get("/api/transcriptions-metadata")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == CHOICE_REQUIRED
    assert fetch.call_count == 1


def test_routes_refuse_a_user_with_no_jurisdiction(engine, dictation_app, mocker):
    fetch = mocker.patch("transcribe_api.api.routes_dictation.fetch_transcriptions_metadata", return_value=[])
    response = _as(dictation_app, _judge(engine)).get("/api/transcriptions-metadata")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == NO_JURISDICTION
    fetch.assert_not_called()


def test_document_content_is_the_iac_templates_only(engine, dictation_app, mocker):
    iac = _jurisdiction(engine, "IAC", settings={"template": "iac_decision"})
    et = _jurisdiction(engine, "ET")
    user = _judge(engine)
    _member(engine, user, iac)
    _member(engine, user, et)
    mocker.patch(
        "transcribe_api.api.routes_dictation.get_document_content",
        return_value={"hearing_types": [], "legal_frameworks": [], "anonymity": {}},
    )
    client = _as(dictation_app, user)

    assert client.get("/api/document-content", headers={"X-Jurisdiction": "IAC"}).status_code == 200
    assert client.get("/api/document-content", headers={"X-Jurisdiction": "ET"}).status_code == 404


def test_the_live_draft_is_the_jurisdictions_own(engine, dictation_app, mocker):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _judge(engine)
    _member(engine, user, iac)
    _member(engine, user, et)
    upsert = mocker.patch("transcribe_api.api.routes_dictation.upsert_live_draft")
    get = mocker.patch("transcribe_api.api.routes_dictation.get_live_draft", return_value=None)
    client = _as(dictation_app, user)
    body = {"transcript": {"general": []}, "form_data": {}}

    assert client.put("/api/live-draft", json=body, headers={"X-Jurisdiction": "ET"}).status_code == 204
    assert upsert.call_args.args[:2] == (user.id, et.id)
    assert client.get("/api/live-draft", headers={"X-Jurisdiction": "IAC"}).status_code == 404
    assert get.call_args.args == (user.id, iac.id)


def test_first_login_through_the_resolver_enrols_the_user(engine, monkeypatch):
    from transcribe_api.domain.auth import dependencies_dictation

    _jurisdiction(engine, "IAC", auto_enrol=True)
    monkeypatch.setattr(dependencies_dictation, "get_engine", lambda: engine)

    resolved = dependencies_dictation._resolve_user(str(uuid4()), "first.time@example.test", ["Judge"])

    assert _memberships(engine, resolved.db_user) == [("IAC", "judge")]


# An upload's transcription is made in the tab's jurisdiction ---------------------------


def test_an_upload_is_transcribed_in_the_jurisdiction_it_was_started_in(engine, dictation_app, mocker):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _judge(engine)
    _member(engine, user, iac)
    _member(engine, user, et)
    mocker.patch("transcribe_api.api.routes_dictation.get_settings").return_value.DIRECT_SDK_ACCESS = False

    response = _as(dictation_app, user).post(
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
    iac = _jurisdiction(engine, "IAC")
    user = _user(engine)
    _member(engine, user, iac)
    assert jurisdictions.upload_jurisdiction_id(uuid4(), user.id) == iac.id
    assert jurisdictions.upload_jurisdiction_id(None, user.id) == iac.id


def test_an_upload_started_in_a_jurisdiction_the_user_has_since_left_is_not_put_there(engine):
    iac, et = _jurisdiction(engine, "IAC"), _jurisdiction(engine, "ET")
    user = _user(engine)
    _member(engine, user, iac)
    upload_id = uuid4()
    jurisdictions.record_upload_jurisdiction(upload_id, et.id)
    assert jurisdictions.upload_jurisdiction_id(upload_id, user.id) == iac.id


def test_the_jurisdiction_of_an_existing_transcription_is_not_looked_up(engine):
    iac = _jurisdiction(engine, "IAC")
    user = _user(engine)
    saved = interface_dictation.save_transcription(Transcription(title="x"), user.id, iac.id)

    def must_not_ask():
        raise AssertionError("looked up")

    assert interface_dictation.load_or_create_transcription(saved.id, user.id, must_not_ask).id == saved.id


def test_only_a_jurisdiction_with_the_decision_template_can_submit_a_decision(engine, dictation_app, mocker):
    et = _jurisdiction(engine, "ET")
    user = _judge(engine)
    _member(engine, user, et)
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

    response = _as(dictation_app, user).post("/api/live-transcription/submission", json=body)

    assert response.status_code == 404
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
