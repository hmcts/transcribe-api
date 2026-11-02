"""DIAAT-343: a startup check reports where the live document content differs from the
bundled defaults. It reads only, never raises and never touches the wording."""

from __future__ import annotations

import copy
import logging

import pytest
from fastapi.testclient import TestClient

from transcribe_api.documents import content_drift
from transcribe_api.documents.content_loader import _load_from_file
from transcribe_api.domain.models_dictation import DocumentContent

WORDING = "Wording an admin typed, which must never reach a log or an event"


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _FakeSession:
    """Serves one document_content row and records any write."""

    def __init__(self, content, writes: list[str]):
        self.content = content
        self.writes = writes
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec(self, statement):
        return _Result(None if self.content is None else DocumentContent(id=1, content=self.content))

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


def _live(monkeypatch, writes, content) -> _FakeSession:
    session = _FakeSession(content, writes)
    monkeypatch.setattr("sqlmodel.Session", lambda engine: session)
    monkeypatch.setattr("transcribe_api.runtime.db_connection.get_engine", lambda: object())
    return session


def test_identical_content_has_no_drift():
    shipped = _load_from_file()
    assert not content_drift.compare_content(copy.deepcopy(shipped), shipped)


def test_compare_finds_edited_live_only_and_default_only_items_by_id():
    shipped = _load_from_file()
    live = copy.deepcopy(shipped)
    live["hearing_types"][0]["title"] = WORDING
    live["legal_frameworks"].append({"id": "added_by_an_admin", "title": WORDING})
    removed = live["anonymity"]["statuses"].pop()

    drift = content_drift.compare_content(live, shipped)

    assert drift.edited == [f"hearing_types/{shipped['hearing_types'][0]['id']}"]
    assert drift.live_only == ["legal_frameworks/added_by_an_admin"]
    assert drift.default_only == [f"anonymity_statuses/{removed['id']}"]


def test_compare_tolerates_missing_collections_and_items_without_an_id():
    shipped = _load_from_file()
    drift = content_drift.compare_content({"hearing_types": [{"title": "no id"}], "anonymity": None}, shipped)
    assert drift.edited == []
    assert drift.live_only == []
    assert len(drift.default_only) == sum(
        len(content_drift._items_by_id(shipped, p)) for p in content_drift.COLLECTIONS.values()
    )


def test_drift_warns_with_ids_and_counts_and_records_the_event(monkeypatch, writes, events, caplog):
    shipped = _load_from_file()
    live = copy.deepcopy(shipped)
    live["hearing_types"][0]["hearing_title"] = WORDING
    live["legal_frameworks"].append({"id": "added_by_an_admin", "content": WORDING})
    _live(monkeypatch, writes, live)

    with caplog.at_level(logging.WARNING, logger=content_drift.logger.name):
        drift = content_drift.check_content_drift()

    assert drift is not None
    assert events == [
        (
            "content.drift_detected",
            {"content.edited_count": 1, "content.live_only_count": 1, "content.default_only_count": 0},
        )
    ]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    message = caplog.text
    assert f"hearing_types/{shipped['hearing_types'][0]['id']}" in message
    assert "legal_frameworks/added_by_an_admin" in message
    assert WORDING not in message
    assert WORDING not in str(events)
    assert writes == []


def test_no_drift_is_silent(monkeypatch, writes, events, caplog):
    _live(monkeypatch, writes, copy.deepcopy(_load_from_file()))

    with caplog.at_level(logging.DEBUG):
        assert content_drift.check_content_drift() is None

    assert caplog.records == []
    assert events == []
    assert writes == []


def test_no_live_row_yet_is_silent_and_not_seeded(monkeypatch, writes, events, caplog):
    _live(monkeypatch, writes, None)

    with caplog.at_level(logging.DEBUG):
        assert content_drift.check_content_drift() is None

    assert caplog.records == []
    assert events == []
    assert writes == []


def test_an_unavailable_database_is_skipped_without_raising(monkeypatch, events, caplog):
    def unavailable():
        raise RuntimeError("could not connect to postgresql://user:secret@db")

    monkeypatch.setattr("transcribe_api.runtime.db_connection.get_engine", unavailable)

    with caplog.at_level(logging.WARNING, logger=content_drift.logger.name):
        assert content_drift.check_content_drift() is None

    assert "skipped" in caplog.text
    assert "secret" not in caplog.text
    assert events == []


def test_a_failure_recording_the_event_does_not_raise(monkeypatch, writes):
    live = copy.deepcopy(_load_from_file())
    live["hearing_types"].pop()
    _live(monkeypatch, writes, live)

    def broken(*args, **kwargs):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("hmcts_otel.record_event", broken)

    assert content_drift.check_content_drift() is None


def test_startup_runs_the_check_after_the_migrations(monkeypatch):
    calls: list[str] = []
    monkeypatch.setenv("RUN_MIGRATIONS", "true")
    monkeypatch.setattr("transcribe_api.runtime.migrations.run_migrations", lambda: calls.append("migrate"))
    monkeypatch.setattr(content_drift, "check_content_drift", lambda: calls.append("drift"))
    from transcribe_api.api.app import create_app

    with TestClient(create_app()):
        pass

    assert calls == ["migrate", "drift"]


def test_the_read_is_bounded_by_database_side_timeouts(monkeypatch, writes, events):
    session = _live(monkeypatch, writes, _load_from_file())

    content_drift.check_content_drift()

    assert session.executed == ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '5s'"]
    assert writes == []
