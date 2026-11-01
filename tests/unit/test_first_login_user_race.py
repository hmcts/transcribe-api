"""Concurrent first logins for one user must both resolve to the same User (DIAAT-289).

AAT, 2026-10-28: two first-login requests for the same user both missed the
lookup in _resolve_user and both inserted; the loser's UniqueViolation on
ix_user_azure_user_id surfaced as a 500 from GET /api/user/onboarding-status.

Both resolvers run against a real database, so the unique index, the rollback
and the re-select are exercised rather than mocked. The SQLite tests always
run; the Postgres test races two threads for real and needs TEST_POSTGRES_URL
(a throwaway Postgres), like tests/unit/test_migrations.py.
"""

from __future__ import annotations

import importlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlmodel import Session, SQLModel, create_engine, select

from transcribe_api.domain.models_dictation import Jurisdiction, JurisdictionMembership, User

RESOLVERS = [
    pytest.param("transcribe_api.domain.auth.dependencies_dictation", id="dictation"),
    pytest.param("transcribe_api.domain.auth.dependencies_recording", id="recording"),
]

AZURE_USER_ID = "3f2a9c1e-0000-4000-8000-000000000001"
EMAIL = "judge@example.test"


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):
    return "JSON"


# The user and, since DIAAT-322, the jurisdiction tables a first login writes to.
FIRST_LOGIN_TABLES = [User.__table__, Jurisdiction.__table__, JurisdictionMembership.__table__]


@pytest.fixture
def sqlite_engine(tmp_path):
    # A file rather than :memory: so the winning request gets its own connection.
    engine = create_engine(f"sqlite:///{tmp_path / 'users.db'}")
    SQLModel.metadata.create_all(engine, tables=FIRST_LOGIN_TABLES)
    yield engine
    engine.dispose()


def _commit_user(engine) -> User:
    with Session(engine) as session:
        user = User(email=EMAIL, azure_user_id=AZURE_USER_ID, role="Normal")
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def _users(engine) -> list[User]:
    with Session(engine) as session:
        return list(session.exec(select(User)).all())


def _losing_session(engine) -> type[Session]:
    """Session class for the request that loses the race.

    Its lookup runs against the empty table; the winner then commits the same
    user before this request's INSERT is flushed. Only a flush with the insert
    pending: the lookup's autoflush, with nothing pending, comes first and would
    commit the winner before the lookup, which would then simply find it.
    """

    class LosingSession(Session):
        winner: User | None = None
        rolled_back = False

        def flush(self, *args, **kwargs):
            if LosingSession.winner is None and self.new:
                LosingSession.winner = _commit_user(engine)
            super().flush(*args, **kwargs)

        def rollback(self):
            LosingSession.rolled_back = True
            super().rollback()

    return LosingSession


@pytest.mark.parametrize("module_name", RESOLVERS)
def test_a_first_login_creates_the_user(module_name, sqlite_engine):
    deps = importlib.import_module(module_name)

    with patch.object(deps, "get_engine", return_value=sqlite_engine):
        result = deps._resolve_user(AZURE_USER_ID, EMAIL, ["Judge"])

    [stored] = _users(sqlite_engine)
    assert result.db_user.id == stored.id
    assert (stored.email, stored.azure_user_id, stored.role) == (EMAIL, AZURE_USER_ID, "Judge")
    assert stored.has_completed_onboarding is False
    assert result.app_roles == ["Judge"]


@pytest.mark.parametrize("module_name", RESOLVERS)
def test_the_request_that_loses_the_insert_gets_the_winners_row(module_name, sqlite_engine):
    deps = importlib.import_module(module_name)
    losing_session = _losing_session(sqlite_engine)

    with (
        patch.object(deps, "get_engine", return_value=sqlite_engine),
        patch.object(deps, "Session", losing_session),
    ):
        result = deps._resolve_user(AZURE_USER_ID, EMAIL, ["Normal"])

    # The race really happened: this request's insert failed and it recovered.
    assert losing_session.rolled_back
    assert result.db_user.id == losing_session.winner.id
    assert result.db_user.azure_user_id == AZURE_USER_ID
    assert len(_users(sqlite_engine)) == 1


@pytest.mark.parametrize("module_name", RESOLVERS)
def test_an_unrelated_integrity_error_is_not_mistaken_for_the_race(module_name, sqlite_engine):
    # A role outside ck_user_role_valid fails the INSERT with no row to fall back on.
    deps = importlib.import_module(module_name)

    with (
        patch.object(deps, "get_engine", return_value=sqlite_engine),
        patch.object(deps, "get_valid_roles", return_value={"Normal": "Clerk"}),
        pytest.raises(IntegrityError, match="ck_user_role_valid"),
    ):
        deps._resolve_user(AZURE_USER_ID, EMAIL, ["Clerk"])

    assert _users(sqlite_engine) == []


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
@pytest.mark.parametrize("module_name", RESOLVERS)
def test_concurrent_first_logins_on_postgres_resolve_to_one_user(module_name):
    deps = importlib.import_module(module_name)
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    SQLModel.metadata.create_all(engine, tables=FIRST_LOGIN_TABLES)

    # Both requests must miss the lookup before either inserts. The insert is flushed
    # before the commit (the jurisdictions go in the same transaction, DIAAT-322), so
    # they meet at the flush of their insert: the second then waits on the first's
    # row. Not at the lookup's autoflush (nothing pending), which comes first: one
    # could then insert and commit before the other looked, and there is no race.
    both_looked_up = threading.Barrier(2, timeout=10)

    rollbacks = []

    class RacingSession(Session):
        def flush(self, *args, **kwargs):
            if self.new and not getattr(self, "_met", False):
                self._met = True
                both_looked_up.wait()
            super().flush(*args, **kwargs)

        def rollback(self):
            rollbacks.append(1)
            super().rollback()

    try:
        with (
            patch.object(deps, "get_engine", return_value=engine),
            patch.object(deps, "Session", RacingSession),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(deps._resolve_user, AZURE_USER_ID, EMAIL, ["Normal"]) for _ in range(2)]
            first, second = (future.result() for future in futures)

        assert first.db_user.id == second.db_user.id
        assert len(_users(engine)) == 1
        # The race really happened: one insert failed and that request recovered.
        assert len(rollbacks) == 1
    finally:
        engine.dispose()
