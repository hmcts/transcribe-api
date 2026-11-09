"""DIAAT-388: the dictation off-switch, enforced server-side as part of ``dictation.use``.

The flag layer (domain/feature_flags.py): the ``system_feature_flag`` row ``dictation_enabled`` if
there is one, else the ``DICTATION_ENABLED`` setting. With it off every dictation route refuses with
403 ``dictation_disabled`` and an audited denial; hearing recordings, documents and everything else
are unaffected. ``GET /api/flags`` says what is on.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

from tests.unit.test_permission_matrix import Route, guarded, routes_of, sign_in_as
from transcribe_api.domain import feature_flags
from transcribe_api.domain.auth import authorisation
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_dictation import AuthenticatedUser as DictationUser
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser as RecordingUser
from transcribe_api.domain.auth.authorisation import declared_permissions
from transcribe_api.domain.models_dictation import SystemFeatureFlag, User
from transcribe_api.runtime import settings_dictation


def _set_default(monkeypatch, enabled: bool | None) -> None:
    if enabled is None:
        monkeypatch.delenv("DICTATION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("DICTATION_ENABLED", "true" if enabled else "false")
    settings_dictation.get_settings.cache_clear()


@pytest.fixture
def flag_table(jurisdiction_db, monkeypatch):
    """The system_feature_flag table on SQLite, read by the real flag layer."""
    SQLModel.metadata.create_all(jurisdiction_db, tables=[SystemFeatureFlag.__table__])
    monkeypatch.setattr(feature_flags, "get_engine", lambda: jurisdiction_db)

    def store(value: bool) -> None:
        with Session(jurisdiction_db) as session:
            session.merge(SystemFeatureFlag(name="dictation_enabled", value=value, updated_at=datetime.now(UTC)))
            session.commit()

    return store


# --- the flag layer ---------------------------------------------------------------------------


@pytest.mark.real_jurisdictions
@pytest.mark.real_feature_flags
def test_with_no_row_the_setting_decides_and_it_defaults_to_on(flag_table, monkeypatch):
    _set_default(monkeypatch, None)
    assert feature_flags.is_dictation_enabled() is True
    _set_default(monkeypatch, True)
    assert feature_flags.is_dictation_enabled() is True
    _set_default(monkeypatch, False)
    assert feature_flags.is_dictation_enabled() is False


@pytest.mark.real_jurisdictions
@pytest.mark.real_feature_flags
@pytest.mark.parametrize("setting", [True, False])
@pytest.mark.parametrize("row", [True, False])
def test_a_row_overrides_the_setting_either_way(flag_table, monkeypatch, setting, row):
    _set_default(monkeypatch, setting)
    flag_table(row)
    assert feature_flags.is_dictation_enabled() is row


@pytest.mark.real_jurisdictions
@pytest.mark.real_feature_flags
def test_a_change_to_the_row_applies_to_the_next_request_with_no_restart(flag_table, monkeypatch):
    _set_default(monkeypatch, True)
    flag_table(False)
    assert feature_flags.is_dictation_enabled() is False
    flag_table(True)
    assert feature_flags.is_dictation_enabled() is True


@pytest.mark.real_feature_flags
@pytest.mark.parametrize("setting", [True, False])
def test_if_the_database_cannot_be_read_the_default_applies(monkeypatch, setting):
    def down():
        msg = "database down"
        raise RuntimeError(msg)

    monkeypatch.setattr(feature_flags, "get_engine", down)
    _set_default(monkeypatch, setting)
    assert feature_flags.is_dictation_enabled() is setting


def test_an_unknown_flag_is_an_error_not_an_off_switch():
    with pytest.raises(KeyError):
        feature_flags.flag_value("no_such_flag")


# --- every dictation route, off and on ----------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    from transcribe_api.api.app import create_app

    return create_app()


@pytest.fixture(scope="module")
def routes(app) -> list[Route]:
    return routes_of(app)[0]


def _only_dictation_use(route: Route) -> bool:
    return route.permission_sets() == {frozenset({"dictation.use"})}


def _judge(cls):
    return cls(db_user=User(id=uuid4(), email="j@example.test", azure_user_id="oid"), ctx=Context("IAC", "Judge"))


@pytest.fixture
def audit_rows(monkeypatch):
    rows: list = []
    monkeypatch.setattr(authorisation, "write_audit_event", rows.append)
    return rows


def _as_judge(app, routes):
    sign_in_as(app, routes, {"dictation": _judge(DictationUser), "recording": _judge(RecordingUser)})
    return TestClient(app, raise_server_exceptions=False)


def _url(route: Route) -> str:
    return re.sub(r"\{\w+(:\w+)?\}", lambda _m: str(uuid4()), route.path)


def test_with_dictation_off_every_dictation_route_refuses_and_audits_the_refusal(app, routes, monkeypatch, audit_rows):
    _set_default(monkeypatch, False)
    dictation_routes = [r for r in guarded(routes) if _only_dictation_use(r)]
    assert len(dictation_routes) >= 25  # not vacuous: the dictation product's routes
    client = _as_judge(app, routes)
    try:
        wrong = []
        for route in dictation_routes:
            audit_rows.clear()
            response = client.request(route.method, _url(route))
            detail = response.json().get("detail")
            if response.status_code != 403 or detail.get("code") != "dictation_disabled":
                wrong.append((route.key, response.status_code, detail))
            elif len(audit_rows) != 1 or audit_rows[0].detail != {"reason": "dictation_disabled"}:
                wrong.append((route.key, "not audited once as dictation_disabled", audit_rows))
        assert wrong == []
    finally:
        app.dependency_overrides.clear()


def test_with_dictation_off_the_hearing_recordings_documents_and_everything_else_are_unaffected(
    routes, monkeypatch, audit_rows
):
    _set_default(monkeypatch, False)
    judge = {"dictation": _judge(DictationUser), "recording": _judge(RecordingUser)}
    for route in guarded(routes):
        if _only_dictation_use(route):
            continue
        (dependency,) = {c for c in route.calls() if declared_permissions(c) is not None}
        area = "recording" if route.path.startswith("/api/v1") else "dictation"

        class _Request:
            method = route.method
            client = None

            class url:
                path = route.path

        try:
            asyncio.run(dependency(request=_Request(), current_user=judge[area]))
        except Exception as refused:
            assert getattr(refused, "detail", {}).get("code") != "dictation_disabled", route.key
    assert [r for r in audit_rows if getattr(r, "detail", None) == {"reason": "dictation_disabled"}] == []


def test_with_dictation_on_a_judge_gets_through_the_permission_check(app, routes, monkeypatch, audit_rows):
    """Both settings are covered: on is the default, and the same routes are refused with it off (above)."""
    _set_default(monkeypatch, True)
    route = next(r for r in guarded(routes) if r.path == "/api/get-speech-token")

    class _Request:
        method = "GET"
        client = None

        class url:
            path = "/api/get-speech-token"

    (dependency,) = {c for c in route.calls() if declared_permissions(c) is not None}
    judge = _judge(DictationUser)
    assert asyncio.run(dependency(request=_Request(), current_user=judge)) is judge
    assert audit_rows == []


def test_a_row_set_by_a_system_administrator_switches_it_off_in_the_running_api(routes, monkeypatch, audit_rows):
    """The table wins over the setting: on by default, off by the row, with no restart."""
    _set_default(monkeypatch, True)
    stored = {"value": None}
    monkeypatch.setattr(feature_flags, "_stored_value", lambda name: stored["value"])
    route = next(r for r in guarded(routes) if r.path == "/api/get-speech-token")
    (dependency,) = {c for c in route.calls() if declared_permissions(c) is not None}
    judge = _judge(DictationUser)

    class _Request:
        method = "GET"
        client = None

        class url:
            path = "/api/get-speech-token"

    assert asyncio.run(dependency(request=_Request(), current_user=judge)) is judge
    stored["value"] = False
    with pytest.raises(Exception) as refused:
        asyncio.run(dependency(request=_Request(), current_user=judge))
    assert (refused.value.status_code, refused.value.detail["code"]) == (403, "dictation_disabled")
    stored["value"] = True
    assert asyncio.run(dependency(request=_Request(), current_user=judge)) is judge


def test_a_role_without_dictation_use_still_gets_permission_denied_not_the_switch(app, routes, monkeypatch, audit_rows):
    _set_default(monkeypatch, False)
    ltm = DictationUser(
        db_user=User(id=uuid4(), email="l@example.test", azure_user_id="oid"), ctx=Context("IAC", "LegalTextManager")
    )
    sign_in_as(app, routes, {"dictation": ltm, "recording": ltm})
    try:
        refused = TestClient(app, raise_server_exceptions=False).get("/api/get-speech-token")
    finally:
        app.dependency_overrides.clear()
    assert refused.json()["detail"]["code"] == "permission_denied"


# --- GET /api/flags -------------------------------------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("who", ["no-role", "sysadmin-only", "judge"])
def test_flags_are_visible_to_any_signed_in_user_with_no_context_needed(app, routes, monkeypatch, enabled, who):
    _set_default(monkeypatch, enabled)
    user = DictationUser(
        db_user=User(id=uuid4(), email="x@example.test", azure_user_id="oid"),
        ctx=Context("IAC", "Judge") if who == "judge" else None,
        sysadmin=who == "sysadmin-only",
    )
    sign_in_as(app, [r for r in routes if r.path == "/api/flags"], {"dictation": user})
    try:
        response = TestClient(app, raise_server_exceptions=False).get("/api/flags")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"dictation_enabled": enabled}
    assert response.headers["cache-control"] == "no-store"


def test_flags_need_a_sign_in(app):
    assert TestClient(app, raise_server_exceptions=False).get("/api/flags").status_code == 401


def test_the_default_is_on_without_any_configuration(monkeypatch):
    _set_default(monkeypatch, None)
    assert settings_dictation.get_settings().DICTATION_ENABLED is True
