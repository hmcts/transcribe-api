"""DIAAT-343: a startup check reports where the live IAC settings differ from the ones it
was created with. It reads only, never raises and never reports the wording."""

from __future__ import annotations

import copy
import logging

import pytest

from tests.unit.jurisdiction_fixtures import load_migration
from transcribe_api.domain import jurisdiction_settings_drift as drift_check
from transcribe_api.domain.models_dictation import Jurisdiction

WORDING = "Wording an admin typed, which must never reach a log or an event"


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _FakeSession:
    """Serves the jurisdiction rows it was given and records any write."""

    def __init__(self, rows: list[Jurisdiction], writes: list[str]):
        self.rows = rows
        self.writes = writes
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec(self, statement):
        # The check selects by code: the fake answers for the code in the statement.
        code = next(iter(statement.compile().params.values()))
        return _Result(next((r for r in self.rows if r.code == code), None))

    def execute(self, statement):
        self.executed.append(str(statement))

    def add(self, *args):
        self.writes.append("add")

    def commit(self):
        self.writes.append("commit")

    def delete(self, *args):
        self.writes.append("delete")


@pytest.fixture
def writes() -> list[str]:
    return []


@pytest.fixture
def events(monkeypatch) -> list[tuple]:
    recorded: list[tuple] = []
    monkeypatch.setattr(
        "hmcts_otel.record_event", lambda name, attributes=None, **kw: recorded.append((name, attributes))
    )
    return recorded


def _live(monkeypatch, writes, *rows: Jurisdiction) -> _FakeSession:
    session = _FakeSession(list(rows), writes)
    monkeypatch.setattr("sqlmodel.Session", lambda engine: session)
    monkeypatch.setattr("transcribe_api.runtime.db_connection.get_engine", lambda: object())
    return session


def _row(code: str, settings: dict) -> Jurisdiction:
    return Jurisdiction(code=code, name=f"{code} name", settings=settings)


def _shipped() -> dict:
    return copy.deepcopy(drift_check.SHIPPED_IAC_SETTINGS)


def test_the_shipped_copy_is_the_frozen_seed_of_migration_0004():
    module = load_migration("0004_jurisdictions.py")
    assert drift_check.SHIPPED_IAC_SETTINGS == module.IAC_SETTINGS
    assert drift_check.SHIPPED_CODE == module.IAC_CODE


def test_identical_settings_have_no_drift():
    assert not drift_check.compare_settings(_shipped(), drift_check.SHIPPED_IAC_SETTINGS)


def test_compare_finds_every_kind_of_difference_by_key():
    live = _shipped()
    live["wording"]["home.intro"] = WORDING
    live["wording"]["nav.recordings"] = WORDING
    live["wording"]["records.open"] = WORDING
    live["wording"]["not.shipped"] = WORDING
    del live["wording"]["records.no_match"]
    live["template"] = None
    live["features"] = {"hearing_recordings": False, "added_flag": True}

    drift = drift_check.compare_settings(live, drift_check.SHIPPED_IAC_SETTINGS)

    assert drift.wording_edited == ["home.intro", "nav.recordings", "records.open"]
    assert drift.wording_live_only == ["not.shipped"]
    assert drift.wording_default_only == ["records.no_match"]
    assert drift.template_changed
    assert drift.features_changed == ["added_flag", "hearing_recordings"]


def test_compare_tolerates_missing_or_malformed_settings():
    drift = drift_check.compare_settings(
        {"wording": ["not", "a", "dict"], "features": None}, drift_check.SHIPPED_IAC_SETTINGS
    )
    assert drift.wording_edited == []
    assert drift.wording_live_only == []
    assert len(drift.wording_default_only) == len(drift_check.SHIPPED_IAC_SETTINGS["wording"])
    assert drift.template_changed
    assert drift.features_changed == ["hearing_recordings"]


def test_drift_warns_with_key_names_and_counts_and_records_the_event(monkeypatch, writes, events, caplog):
    live = _shipped()
    live["wording"]["home.intro"] = WORDING
    live["wording"]["not.shipped"] = WORDING
    live["template"] = "something_else"
    live["features"]["hearing_recordings"] = False
    _live(monkeypatch, writes, _row("IAC", live))

    with caplog.at_level(logging.WARNING, logger=drift_check.logger.name):
        drift = drift_check.check_settings_drift()

    assert drift is not None
    assert events == [
        (
            "settings.drift_detected",
            {
                "settings.wording_edited_count": 1,
                "settings.wording_live_only_count": 1,
                "settings.wording_default_only_count": 0,
                "settings.template_changed": True,
                "settings.features_changed_count": 1,
            },
        )
    ]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "home.intro" in caplog.text
    assert "not.shipped" in caplog.text
    assert "hearing_recordings" in caplog.text
    assert WORDING not in caplog.text
    assert "something_else" not in caplog.text
    assert WORDING not in str(events)
    assert writes == []


def test_the_iac_is_matched_by_code_whatever_other_jurisdictions_hold(monkeypatch, writes, events):
    edited = _shipped()
    edited["wording"]["home.intro"] = WORDING
    _live(monkeypatch, writes, _row("ET", {"wording": {"home.intro": WORDING}}), _row("IAC", _shipped()))

    assert drift_check.check_settings_drift() is None
    assert events == []

    _live(monkeypatch, writes, _row("ET", {}), _row("IAC", edited))
    drift = drift_check.check_settings_drift()
    assert drift is not None
    assert drift.wording_edited == ["home.intro"]


def test_no_drift_is_silent(monkeypatch, writes, events, caplog):
    _live(monkeypatch, writes, _row("IAC", _shipped()))

    with caplog.at_level(logging.DEBUG):
        assert drift_check.check_settings_drift() is None

    assert caplog.records == []
    assert events == []
    assert writes == []


def test_no_iac_row_is_silent_and_not_created(monkeypatch, writes, events, caplog):
    _live(monkeypatch, writes, _row("ET", {}))

    with caplog.at_level(logging.DEBUG):
        assert drift_check.check_settings_drift() is None

    assert caplog.records == []
    assert events == []
    assert writes == []


def test_an_unavailable_database_is_skipped_without_raising(monkeypatch, events, caplog):
    def unavailable():
        raise RuntimeError("could not connect to postgresql://user:secret@db")

    monkeypatch.setattr("transcribe_api.runtime.db_connection.get_engine", unavailable)

    with caplog.at_level(logging.WARNING, logger=drift_check.logger.name):
        assert drift_check.check_settings_drift() is None

    assert "skipped" in caplog.text
    assert "secret" not in caplog.text
    assert events == []


def test_a_failure_recording_the_event_does_not_raise(monkeypatch, writes):
    live = _shipped()
    live["template"] = None
    _live(monkeypatch, writes, _row("IAC", live))

    def broken(*args, **kwargs):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("hmcts_otel.record_event", broken)

    assert drift_check.check_settings_drift() is None


def test_the_read_is_bounded_by_database_side_timeouts(monkeypatch, writes, events):
    session = _live(monkeypatch, writes, _row("IAC", _shipped()))

    drift_check.check_settings_drift()

    assert session.executed == ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '5s'"]
    assert writes == []
