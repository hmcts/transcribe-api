"""Database migrations: one Alembic history, applied at API startup.

The offline tests render the migration SQL without a database. The integration
test needs a real Postgres (TEST_POSTGRES_URL) and is skipped without one; run
it locally against a throwaway container before changing the baseline.
"""

from __future__ import annotations

import io
import os
import re

import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from transcribe_api.runtime import migrations

POSTGRES_URL = "postgresql://user@localhost/db"

EXPECTED_TABLES = {
    "audit_log",
    "caller",
    "correction_dataset_entry",
    "document_content",
    "jurisdiction",
    "jurisdiction_membership",
    "live_transcript_draft",
    "minuteversion",
    "tag",
    "transcription",
    "transcription_job",
    "transcription_tag_association",
    "transcriptionjob",
    "upload_jurisdiction",
    "user",
}


# Columns that were `timestamp with time zone` in the old schemas (see
# docs/data-migration/schema-mapping.md). 0001 created them naive; 0002 restores
# them. Every other timestamp column was naive in the old schemas and stays so.
AWARE_TIMESTAMP_COLUMNS = {
    "audit_log": ["timestamp"],
    "caller": ["created_datetime", "updated_datetime"],
    "correction_dataset_entry": ["created_datetime", "updated_datetime"],
    "document_content": ["updated_at"],
    "live_transcript_draft": ["created_datetime", "updated_datetime"],
    "transcription_job": ["created_datetime", "updated_datetime", "webhook_dispatched_at"],
}

# Timestamp columns added after 0002, created timestamptz from the start.
ADDED_AWARE_TIMESTAMP_COLUMNS = {
    "transcription_job": ["processing_started_datetime", "stuck_reported_at"],  # 0003
    "jurisdiction": ["created_datetime", "updated_datetime"],  # 0004
    "jurisdiction_membership": ["created_datetime"],  # 0004
    "upload_jurisdiction": ["created_datetime"],  # 0004
}

# Dictation migration a8f2c9d5e1b3 (fix for 15-20s p95 latency); 0001 lost them.
FK_INDEXES = {
    "ix_transcription_user_id": ("transcription", "user_id"),
    "ix_minuteversion_transcription_id": ("minuteversion", "transcription_id"),
    "ix_transcriptionjob_transcription_id": ("transcriptionjob", "transcription_id"),
}


def _offline_sql(monkeypatch, revision: str = "head", downgrade: bool = False) -> str:
    monkeypatch.setenv("DATABASE_CONNECTION_STRING", POSTGRES_URL)
    buffer = io.StringIO()
    config = migrations.alembic_config()
    config.output_buffer = buffer
    from alembic import command

    if downgrade:
        command.downgrade(config, revision, sql=True)
    else:
        command.upgrade(config, revision, sql=True)
    return buffer.getvalue()


def _alter_type_sql(table: str, column: str, type_sql: str) -> re.Pattern[str]:
    return re.compile(
        rf'ALTER TABLE "?{table}"? ALTER COLUMN "?{column}"? TYPE {type_sql} '
        rf"""USING "?{column}"? AT TIME ZONE 'UTC'"""
    )


def test_there_is_one_history_with_one_head():
    script = ScriptDirectory.from_config(migrations.alembic_config())
    assert script.get_bases() == ["0001"]
    assert script.get_heads() == ["0007"]


def test_baseline_creates_every_table(monkeypatch):
    sql = _offline_sql(monkeypatch)
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table} " in sql or f'CREATE TABLE "{table}" ' in sql, table


def test_baseline_keeps_the_schema_rules_that_only_old_migrations_had(monkeypatch):
    sql = _offline_sql(monkeypatch)
    # dictation 8e2a5d9f3b7c: user.role limited to the app roles
    assert "CONSTRAINT ck_user_role_valid CHECK" in sql
    # recording 007: GIN index on dialogue_entries
    assert "ix_transcription_job_dialogue_entries_gin" in sql
    assert "USING gin (dialogue_entries)" in sql
    # recording 012: idempotency for JWT callers
    assert "CREATE UNIQUE INDEX ix_transcription_job_user_idempotency" in sql
    assert "WHERE user_id IS NOT NULL AND idempotency_key IS NOT NULL" in sql


def test_0002_restores_the_dictation_foreign_key_indexes(monkeypatch):
    sql = _offline_sql(monkeypatch, "0001:0002")
    for name, (table, column) in FK_INDEXES.items():
        assert f"CREATE INDEX {name} ON {table} ({column})" in sql, name


def test_0002_restores_timezones_reading_existing_values_as_utc(monkeypatch):
    sql = _offline_sql(monkeypatch, "0001:0002")
    for table, columns in AWARE_TIMESTAMP_COLUMNS.items():
        for column in columns:
            assert _alter_type_sql(table, column, "TIMESTAMP WITH TIME ZONE").search(sql), f"{table}.{column}"
    # Nothing else changes type: the columns that were naive stay naive.
    altered = len(re.findall(r"ALTER COLUMN \S+ TYPE", sql))
    assert altered == sum(len(c) for c in AWARE_TIMESTAMP_COLUMNS.values())


def test_0002_downgrade_reverses_it(monkeypatch):
    sql = _offline_sql(monkeypatch, "0002:0001", downgrade=True)
    for table, columns in AWARE_TIMESTAMP_COLUMNS.items():
        for column in columns:
            assert _alter_type_sql(table, column, "TIMESTAMP WITHOUT TIME ZONE").search(sql), f"{table}.{column}"
    for name, (table, _column) in FK_INDEXES.items():
        assert f"DROP INDEX {name}" in sql, name


def test_the_models_declare_the_restored_timezones_and_indexes():
    """Without a database: the models must say what 0002 does, or autogenerate drifts."""
    from sqlalchemy import DateTime

    tables = migrations.target_metadata().tables
    for table in tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime):
                expected = column.name in (
                    AWARE_TIMESTAMP_COLUMNS.get(table.name, []) + ADDED_AWARE_TIMESTAMP_COLUMNS.get(table.name, [])
                )
                assert column.type.timezone is expected, f"{table.name}.{column.name}"
    for name, (table, column) in FK_INDEXES.items():
        index = next((i for i in tables[table].indexes if i.name == name), None)
        assert index is not None, name
        assert [c.name for c in index.columns] == [column]
        assert not index.unique


# 0003: DIAAT-205 observability columns -------------------------------------------------

OBSERVABILITY_COLUMNS = {
    "audit_log": {
        "outcome": "VARCHAR(16)",
        "object_type": "VARCHAR(32)",
        "object_id": "VARCHAR(64)",
        "request_id": "VARCHAR(64)",
    },
    "transcription_job": {
        "trace_parent": "VARCHAR(64)",
        "processing_started_datetime": "TIMESTAMP WITH TIME ZONE",
        "stuck_reported_at": "TIMESTAMP WITH TIME ZONE",
        "provenance": "JSONB",
    },
    "transcriptionjob": {"provenance": "JSONB"},
}


def test_0003_adds_the_observability_columns_nullable(monkeypatch):
    sql = _offline_sql(monkeypatch, "0002:0003")
    for table, columns in OBSERVABILITY_COLUMNS.items():
        for column, type_sql in columns.items():
            # Nullable with no default: existing rows are untouched.
            assert f"ALTER TABLE {table} ADD COLUMN {column} {type_sql};" in sql, f"{table}.{column}"
    assert "CREATE INDEX ix_audit_log_object_id ON audit_log (object_id)" in sql
    assert "CREATE INDEX ix_audit_log_request_id ON audit_log (request_id)" in sql
    assert "ALTER TABLE audit_log ALTER COLUMN email DROP NOT NULL" in sql
    # The recording service's own audit table is not created: it writes audit_log.
    assert "CREATE TABLE" not in sql


def test_0003_downgrade_reverses_it(monkeypatch):
    sql = _offline_sql(monkeypatch, "0003:0002", downgrade=True)
    for table, columns in OBSERVABILITY_COLUMNS.items():
        for column in columns:
            assert f"ALTER TABLE {table} DROP COLUMN {column};" in sql, f"{table}.{column}"
    # Rows written without an email get an empty one before NOT NULL comes back.
    assert sql.index("UPDATE audit_log SET email = '' WHERE email IS NULL") < sql.index(
        "ALTER TABLE audit_log ALTER COLUMN email SET NOT NULL"
    )


def test_the_models_declare_the_observability_columns():
    tables = migrations.target_metadata().tables
    for table, columns in OBSERVABILITY_COLUMNS.items():
        for column in columns:
            assert tables[table].columns[column].nullable, f"{table}.{column}"
    assert tables["audit_log"].columns["email"].nullable
    indexed = {next(iter(i.columns)).name for i in tables["audit_log"].indexes}
    assert {"object_id", "request_id"} <= indexed


# 0004: DIAAT-322 jurisdictions ---------------------------------------------------------

IAC_ID = "6a1c0f3e-3b7d-4c2a-9e51-1ac000000001"


def test_0004_creates_the_jurisdiction_tables_and_the_iac(monkeypatch):
    sql = _offline_sql(monkeypatch, "0003:0004")
    assert "CREATE TABLE jurisdiction (" in sql
    assert "CREATE TABLE jurisdiction_membership (" in sql
    assert "CREATE UNIQUE INDEX ix_jurisdiction_code ON jurisdiction (code)" in sql
    assert "CREATE UNIQUE INDEX uq_jurisdiction_membership_pending_email" in sql
    assert "WHERE user_id IS NULL" in sql
    assert "VALUES ('6a1c0f3e-3b7d-4c2a-9e51-1ac000000001', 'IAC', 'Immigration and Asylum Chamber', true, true" in sql
    # Every existing user becomes an IAC judge.
    assert "SELECT gen_random_uuid(), '6a1c0f3e-3b7d-4c2a-9e51-1ac000000001', id, 'judge', now() FROM \"user\"" in sql


def test_0004_puts_every_existing_record_in_the_iac(monkeypatch):
    sql = _offline_sql(monkeypatch, "0003:0004")
    for table in ("transcription", "transcription_job", "live_transcript_draft"):
        # Defaulting to the IAC: the previous release's pods write rows without it.
        assert f"ALTER TABLE {table} ADD COLUMN jurisdiction_id UUID DEFAULT '{IAC_ID}'::uuid;" in sql, table
    assert f"UPDATE transcription SET jurisdiction_id = '{IAC_ID}'" in sql
    # API-key callers' jobs too: BenchNotes served the IAC alone.
    assert f"UPDATE transcription_job SET jurisdiction_id = '{IAC_ID}';" in sql
    assert f"UPDATE live_transcript_draft SET jurisdiction_id = '{IAC_ID}'" in sql
    assert "uq_live_transcript_draft_user_jurisdiction UNIQUE (user_id, jurisdiction_id)" in sql
    assert 'ALTER TABLE "user" ADD COLUMN active_jurisdiction_id UUID;' in sql


def test_0004_is_additive_so_the_previous_release_keeps_working_during_the_rollout(monkeypatch):
    sql = _offline_sql(monkeypatch, "0003:0004")
    # The old pods' upsert key (ON CONFLICT (user_id)) and their inserts without a
    # jurisdiction keep working until 0005.
    assert "live_transcript_draft_user_id_key" not in sql
    assert "SET NOT NULL" not in sql
    assert "DROP " not in sql


def test_0005_puts_the_rollouts_records_in_the_iac_then_allows_a_draft_per_jurisdiction(monkeypatch):
    sql = _offline_sql(monkeypatch, "0004:0005")
    # Users who first signed in through an old pod during the rollout were never enrolled.
    assert "WHERE NOT EXISTS (SELECT 1 FROM jurisdiction_membership m WHERE m.user_id = u.id)" in sql
    for table in ("transcription", "transcription_job", "live_transcript_draft"):
        assert f"UPDATE {table} SET jurisdiction_id = '{IAC_ID}' WHERE jurisdiction_id IS NULL" in sql, table
    assert sql.index("UPDATE live_transcript_draft") < sql.index(
        "ALTER TABLE live_transcript_draft ALTER COLUMN jurisdiction_id SET NOT NULL"
    )
    assert "ALTER TABLE live_transcript_draft DROP CONSTRAINT live_transcript_draft_user_id_key" in sql


def test_0005_downgrade_keeps_one_draft_per_user(monkeypatch):
    sql = _offline_sql(monkeypatch, "0005:0004", downgrade=True)
    assert sql.index(f"DELETE FROM live_transcript_draft WHERE jurisdiction_id <> '{IAC_ID}'") < sql.index(
        "ADD CONSTRAINT live_transcript_draft_user_id_key UNIQUE (user_id)"
    )


def test_0006_gives_the_existing_legal_wording_to_the_iac_and_touches_nothing_else(monkeypatch):
    """DIAAT-342. The one write to document_content is the plain backfill of the new
    column (the live wording is the IAC's, and its content and updated_at are not touched)."""
    sql = _offline_sql(monkeypatch, "0005:0006")
    assert f"UPDATE document_content SET jurisdiction_id = '{IAC_ID}' WHERE id = 1" in sql
    writes = re.findall(r"(?:INSERT INTO|UPDATE|DELETE FROM)\s+document_content\b[^;]*", sql, flags=re.IGNORECASE)
    assert writes == [f"UPDATE document_content SET jurisdiction_id = '{IAC_ID}' WHERE id = 1"], writes
    assert sql.index("UPDATE document_content") < sql.index(
        "ALTER TABLE document_content ALTER COLUMN jurisdiction_id SET NOT NULL"
    )
    assert "ADD CONSTRAINT document_content_jurisdiction_id_key UNIQUE (jurisdiction_id)" in sql
    assert "REFERENCES jurisdiction (id)" in sql
    # Id 1 stays the IAC's.
    assert "setval(seq, 1, true)" in sql
    assert "CHECK (id <> 1 OR jurisdiction_id = " in sql


def test_0006_downgrade_keeps_only_the_iacs_row(monkeypatch):
    sql = _offline_sql(monkeypatch, "0006:0005", downgrade=True)
    assert f"DELETE FROM document_content WHERE jurisdiction_id <> '{IAC_ID}'" in sql
    assert "DROP COLUMN jurisdiction_id" in sql


def test_0004_iac_settings_are_valid_and_set_every_wording_key():
    """The IAC keeps the wording it had: every key set, and the settings validate."""
    from tests.unit.jurisdiction_fixtures import load_migration
    from transcribe_api.domain.jurisdictions import WORDING_KEYS, JurisdictionSettings

    module = load_migration("0004_jurisdictions.py")

    settings = JurisdictionSettings.model_validate(module.IAC_SETTINGS)
    assert set(settings.wording) == set(WORDING_KEYS)
    assert settings.template == "iac_decision"
    assert settings.features.hearing_recordings is True
    assert module.IAC_ID == IAC_ID


def test_0004_downgrade_reverses_it(monkeypatch):
    sql = _offline_sql(monkeypatch, "0004:0003", downgrade=True)
    for table in ("transcription", "transcription_job", "live_transcript_draft"):
        assert f"ALTER TABLE {table} DROP COLUMN jurisdiction_id;" in sql, table
    assert 'ALTER TABLE "user" DROP COLUMN active_jurisdiction_id;' in sql
    assert "DROP TABLE jurisdiction_membership;" in sql
    assert "DROP TABLE jurisdiction;" in sql


# DIAAT-343: a deployment never overwrites what an admin changed in the live service -------
#
# The screen wording and per-jurisdiction settings (jurisdiction.settings) and the legal
# wording (document_content) are edited live. 0004 only inserts the IAC row, and the
# document_content row is seeded by the app when absent, so no migration after 0004 may
# write either table: an upgrade would replace an admin's edit. Run without a database.

LIVE_EDITED_TABLES = ("jurisdiction", "document_content")
_LIVE_WRITE = re.compile(
    r"(?:^|[\s(])(?:INSERT\s+INTO|UPDATE(?:\s+ONLY)?|DELETE\s+FROM(?:\s+ONLY)?|TRUNCATE(?:\s+TABLE)?(?:\s+ONLY)?)\s+"
    r'(?:"?\w+"?\s*\.\s*)?'
    rf'"?({"|".join(LIVE_EDITED_TABLES)})"?(?![\w.])',
    re.IGNORECASE,
)
# TRUNCATE takes a list of tables, any of which may be live: TRUNCATE [TABLE] [ONLY] name [*] [, ...] [options]
_TRUNCATE_TARGETS = re.compile(
    r"(?:^|[\s(])TRUNCATE(?:\s+TABLE)?\s+(.+?)(?:\s+(?:RESTART|CONTINUE|CASCADE|RESTRICT)\b.*)?$",
    re.IGNORECASE | re.DOTALL,
)


def _truncates_a_live_table(statement: str) -> bool:
    match = _TRUNCATE_TARGETS.search(statement)
    if not match:
        return False
    for target in match.group(1).split(","):
        name = re.sub(r"^\s*ONLY\s+", "", target, flags=re.IGNORECASE).strip().rstrip("*").strip()
        if name.split(".")[-1].strip().strip('"').lower() in LIVE_EDITED_TABLES:
            return True
    return False


# Revisions allowed to write a live table, and the one statement shape each may use.
# DIAAT-342 adds document_content.jurisdiction_id in 0006 and backfills that column
# alone: it may set jurisdiction_id (one assignment, optionally with a WHERE) and
# nothing else, so the admin's wording in `content` is never touched.
ALLOWED_LIVE_WRITES = {
    "0006": re.compile(
        r'^\s*UPDATE\s+"?document_content"?\s+SET\s+"?jurisdiction_id"?\s*=\s*[^,]+?(?:\s+WHERE\s.+)?$', re.I | re.S
    ),
}


def _top_level(statement: str) -> str:
    """The statement with string literals emptied and every parenthesised part collapsed
    to (), so a pattern sees only its outermost clauses."""
    flat = re.sub(r"'(?:[^']|'')*'", "''", statement)
    while (collapsed := re.sub(r"\([^()]*\)", "()", flat)) != flat:
        flat = collapsed
    return flat


def _forbidden_live_writes(revision: str, sql: str) -> list[str]:
    """The statements of one migration's SQL that write a live-edited table, less the
    ones its revision is allowed (ALLOWED_LIVE_WRITES)."""
    uncommented = re.sub(r"^\s*--.*$", "", sql, flags=re.MULTILINE)  # "-- Running upgrade" heads the first statement
    statements = (statement.strip() for statement in re.split(r";\s*\n", uncommented))
    writes = [
        statement for statement in statements if _LIVE_WRITE.search(statement) or _truncates_a_live_table(statement)
    ]
    allowed = ALLOWED_LIVE_WRITES.get(revision)

    def is_allowed(statement: str) -> bool:
        # The allowed shape's value or WHERE could hide a second write in a subquery.
        return bool(
            allowed
            and allowed.match(_top_level(statement))
            and len(_LIVE_WRITE.findall(statement)) == 1
            and not _truncates_a_live_table(statement)
        )

    return [statement for statement in writes if not is_allowed(statement)]


def _revisions_after_0004() -> list[tuple[str, str]]:
    """(previous revision, revision) for every migration after 0004, oldest first."""
    script = ScriptDirectory.from_config(migrations.alembic_config())
    history = [(revision.down_revision, revision.revision) for revision in script.walk_revisions()][::-1]
    revisions = [revision for _down, revision in history]
    return history[revisions.index("0004") + 1 :]


def test_no_migration_after_0004_writes_the_live_settings_or_content(monkeypatch):
    for previous, revision in _revisions_after_0004():
        sql = _offline_sql(monkeypatch, f"{previous}:{revision}")
        assert _forbidden_live_writes(revision, sql) == [], revision


def test_the_live_write_guard_sees_what_0004_does(monkeypatch):
    sql = _offline_sql(monkeypatch, "0003:0004")
    [insert] = _forbidden_live_writes("0004", sql)
    assert insert.startswith("INSERT INTO jurisdiction (")


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO jurisdiction (id) VALUES (1)",
        "UPDATE jurisdiction SET settings = '{}'",
        "UPDATE ONLY jurisdiction SET name = 'x'",
        'DELETE FROM "jurisdiction" WHERE code = 1',
        "INSERT INTO document_content (id, content) VALUES (1, '{}')",
        "UPDATE document_content SET content = '{}'",
        "UPDATE document_content SET jurisdiction_id = 'a', content = '{}'",
        "DELETE FROM document_content",
        "TRUNCATE document_content",
        "UPDATE public.document_content SET content = '{}'",
        'UPDATE "public"."jurisdiction" SET settings = \'{}\'',
        "UPDATE ONLY public.jurisdiction SET name = 'x'",
        "DELETE FROM ONLY document_content",
        'DELETE FROM ONLY "public".jurisdiction',
        "INSERT INTO public.jurisdiction (id) VALUES (1)",
        "TRUNCATE TABLE ONLY public.document_content",
        "TRUNCATE transcription, document_content",
        "TRUNCATE TABLE jurisdiction_membership, jurisdiction CASCADE",
        'TRUNCATE transcription, ONLY "public"."jurisdiction" * RESTART IDENTITY',
        "WITH x AS (UPDATE jurisdiction SET settings = '{}' RETURNING id) SELECT * FROM x",
        "WITH x AS (SELECT 1) DELETE FROM document_content",
        "WITH d AS (DELETE FROM document_content RETURNING id) INSERT INTO audit (id) SELECT id FROM d",
    ],
)
def test_the_live_write_guard_catches_every_kind_of_write(statement):
    assert _forbidden_live_writes("0006", f"{statement};\n") == [statement]


def test_the_live_write_guard_sees_a_first_statement_under_the_revision_comment():
    sql = "-- Running upgrade 0005 -> 0006\n\nUPDATE jurisdiction SET settings = '{}';\n"
    assert _forbidden_live_writes("0006", sql) == ["UPDATE jurisdiction SET settings = '{}'"]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO jurisdiction_membership (id) VALUES (1)",
        "UPDATE transcription SET jurisdiction_id = 'a'",
        "ALTER TABLE document_content ADD COLUMN jurisdiction_id UUID",
        "CREATE INDEX ix_document_content_jurisdiction_id ON document_content (jurisdiction_id)",
        "SELECT * FROM jurisdiction",
        "UPDATE public.jurisdiction_membership SET role = 'x'",
        'DELETE FROM "public"."transcription"',
        "UPDATE transcription SET name = (SELECT name FROM jurisdiction LIMIT 1)",
        "DELETE FROM jurisdiction_membership WHERE jurisdiction_id IN (SELECT id FROM jurisdiction)",
        "INSERT INTO jurisdiction_membership (jurisdiction_id) SELECT id FROM jurisdiction",
        "TRUNCATE transcription, jurisdiction_membership CASCADE",
    ],
)
def test_the_live_write_guard_ignores_other_tables_and_schema_changes(statement):
    assert _forbidden_live_writes("0006", f"{statement};\n") == []


def test_only_the_revision_that_backfills_a_new_jurisdiction_column_may_write_document_content():
    backfill = "UPDATE document_content SET jurisdiction_id = '6a1c0f3e-3b7d-4c2a-9e51-1ac000000001'::uuid WHERE id = 1"
    assert _forbidden_live_writes("0006", f"{backfill};\n") == []
    assert _forbidden_live_writes("0007", f"{backfill};\n") == [backfill]
    assert _forbidden_live_writes("0006", "UPDATE jurisdiction SET settings = '{}';\n") != []
    subquery = "UPDATE document_content SET jurisdiction_id = (SELECT id FROM jurisdiction WHERE code = 'IAC')"
    assert _forbidden_live_writes("0006", f"{subquery};\n") == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE document_content SET jurisdiction_id = 'a' WHERE id IN "
        "(WITH u AS (UPDATE document_content SET content = '{}' RETURNING id) SELECT id FROM u)",
        "UPDATE document_content SET jurisdiction_id = "
        "(WITH d AS (DELETE FROM jurisdiction RETURNING id) SELECT id FROM d LIMIT 1)",
        "UPDATE document_content SET jurisdiction_id = (SELECT id FROM jurisdiction WHERE code = 'IAC'), "
        "content = '{}'",
    ],
)
def test_the_allowed_backfill_cannot_carry_another_write(statement):
    assert _forbidden_live_writes("0006", f"{statement};\n") == [statement]


class _FakeConnection:
    def __init__(self, log: list[str]):
        self.log = log

    def execute(self, statement, parameters=None):
        self.log.append(str(statement))

    def commit(self):
        self.log.append("COMMIT")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, log: list[str]):
        self.connection = _FakeConnection(log)

    def connect(self):
        return self.connection


def test_upgrade_runs_under_an_advisory_lock(monkeypatch):
    """Several API pods start at once; only one may migrate at a time."""
    log: list[str] = []
    engine = _FakeEngine(log)
    seen = {}

    def fake_upgrade(config, revision):
        seen["connection"] = config.attributes.get("connection")
        log.append(f"upgrade {revision}")

    monkeypatch.setattr(migrations.command, "upgrade", fake_upgrade)

    migrations.run_migrations(engine)

    lock = next(i for i, s in enumerate(log) if "pg_advisory_lock" in s)
    upgrade = log.index("upgrade head")
    unlock = next(i for i, s in enumerate(log) if "pg_advisory_unlock" in s)
    assert lock < upgrade < unlock
    assert seen["connection"] is engine.connection


def test_lock_is_released_when_the_upgrade_fails(monkeypatch):
    log: list[str] = []

    def failing_upgrade(config, revision):
        raise RuntimeError("bad migration")

    monkeypatch.setattr(migrations.command, "upgrade", failing_upgrade)

    with pytest.raises(RuntimeError, match="bad migration"):
        migrations.run_migrations(_FakeEngine(log))
    assert any("pg_advisory_unlock" in s for s in log)


def _startup_with(monkeypatch, run_migrations_env: str | None, recorder):
    if run_migrations_env is None:
        monkeypatch.delenv("RUN_MIGRATIONS", raising=False)
    else:
        monkeypatch.setenv("RUN_MIGRATIONS", run_migrations_env)
    monkeypatch.setattr(migrations, "run_migrations", recorder)
    from transcribe_api.api.app import create_app

    with TestClient(create_app()):
        pass


def test_startup_migrates_when_enabled(monkeypatch):
    calls = []
    _startup_with(monkeypatch, "true", lambda: calls.append(1))
    assert calls == [1]


def test_startup_does_not_migrate_by_default(monkeypatch):
    calls = []
    _startup_with(monkeypatch, None, lambda: calls.append(1))
    assert calls == []


def test_a_failed_migration_stops_startup(monkeypatch):
    def boom():
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        _startup_with(monkeypatch, "true", boom)


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
def test_baseline_matches_the_models_on_a_real_database():
    """Upgrade an empty database, then compare it with the models: no drift."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))

    migrations.run_migrations(engine)

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, migrations.target_metadata())
    assert diff == []


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
def test_0002_keeps_existing_naive_values_as_utc_whatever_the_session_timezone():
    """Rows written under 0001 hold UTC wall-clock time; 0002 must not shift them."""
    from datetime import UTC, datetime

    from sqlalchemy import create_engine, text

    from alembic import command

    engine = create_engine(os.environ["TEST_POSTGRES_URL"], connect_args={"options": "-c timezone=Europe/London"})
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))

    def migrate(step, revision):
        with engine.begin() as connection:
            config = migrations.alembic_config()
            config.attributes["connection"] = connection
            step(config, revision)

    migrate(command.upgrade, "0001")
    summer_noon_utc = datetime(2026, 7, 1, 12, 0)  # BST, so London is UTC+1
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_log (id, timestamp, event_type, user_id, email, held_roles, required_roles)"
                " VALUES (gen_random_uuid(), :ts, 'denied', 'u', 'e', '[]', '[]')"
            ),
            {"ts": summer_noon_utc},
        )

    migrate(command.upgrade, "head")
    with engine.connect() as connection:
        stored = connection.execute(text("SELECT timestamp FROM audit_log")).scalar_one()
    assert stored == summer_noon_utc.replace(tzinfo=UTC)

    migrate(command.downgrade, "0001")
    with engine.connect() as connection:
        stored = connection.execute(text("SELECT timestamp FROM audit_log")).scalar_one()
        indexes = (
            connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE indexname = ANY(:names)"),
                {"names": list(FK_INDEXES)},
            )
            .scalars()
            .all()
        )
    assert stored == summer_noon_utc
    assert indexes == []


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
def test_0003_keeps_existing_rows_and_allows_app_rows_without_an_email():
    from sqlalchemy import create_engine, text

    from alembic import command

    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))

    def migrate(step, revision):
        with engine.begin() as connection:
            config = migrations.alembic_config()
            config.attributes["connection"] = connection
            step(config, revision)

    migrate(command.upgrade, "0002")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_log (id, timestamp, event_type, user_id, email, held_roles, required_roles)"
                " VALUES (gen_random_uuid(), now(), 'ACCESS_DENIED', 'oid', 'judge@example.test', '[]', '[]')"
            )
        )

    migrate(command.upgrade, "0003")
    with engine.begin() as connection:
        old = connection.execute(text("SELECT email, outcome, object_id FROM audit_log")).one()
        assert tuple(old) == ("judge@example.test", None, None)
        connection.execute(
            text(
                "INSERT INTO audit_log (id, timestamp, event_type, user_id, held_roles, required_roles,"
                " outcome, object_type, object_id, request_id)"
                " VALUES (gen_random_uuid(), now(), 'transcript.view', :uid, '[]', '[]',"
                " 'success', 'transcription', :oid, :rid)"
            ),
            {
                "uid": "3f2a9c1e-8b7d-4e6f-9a1b-2c3d4e5f6a7b",
                "oid": "0f8fad5b-d9cb-469f-a165-70867728950e",
                "rid": "0f8fad5bd9cb469fa16570867728950e",
            },
        )

    migrate(command.downgrade, "0002")
    with engine.connect() as connection:
        emails = sorted(connection.execute(text("SELECT email FROM audit_log")).scalars().all())
    assert emails == ["", "judge@example.test"]


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
def test_0004_and_0005_put_existing_users_and_records_in_the_iac_and_reverse():
    from sqlalchemy import create_engine, text

    from alembic import command

    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))

    def migrate(step, revision):
        with engine.begin() as connection:
            config = migrations.alembic_config()
            config.attributes["connection"] = connection
            step(config, revision)

    user_id = "3f2a9c1e-8b7d-4e6f-9a1b-2c3d4e5f6a7b"
    late_user_id = "4f2a9c1e-8b7d-4e6f-9a1b-2c3d4e5f6a7c"
    migrate(command.upgrade, "0003")
    with engine.begin() as connection:
        connection.execute(
            text(
                'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding, role)'
                " VALUES (:uid, now(), 'judge@example.test', 'oid-1', true, 'Judge')"
            ),
            {"uid": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO transcription (id, created_datetime, user_id, title)"
                " VALUES (gen_random_uuid(), now(), :uid, 'A v B')"
            ),
            {"uid": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO live_transcript_draft (id, created_datetime, user_id, transcript, form_data)"
                " VALUES (gen_random_uuid(), now(), :uid, '{}', '{}')"
            ),
            {"uid": user_id},
        )

    migrate(command.upgrade, "0004")
    with engine.begin() as connection:
        # The previous release, still running during the rollout, saves a draft with
        # its upsert key and no jurisdiction, and makes a transcription without one.
        connection.execute(
            text(
                "INSERT INTO live_transcript_draft (id, created_datetime, user_id, transcript, form_data)"
                " VALUES (gen_random_uuid(), now(), :uid, '{}', '{}')"
                " ON CONFLICT (user_id) DO UPDATE SET transcript = EXCLUDED.transcript"
            ),
            {"uid": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO transcription (id, created_datetime, user_id, title)"
                " VALUES (gen_random_uuid(), now(), :uid, 'C v D')"
            ),
            {"uid": user_id},
        )
        # A judge's first sign-in through an old pod: no membership.
        connection.execute(
            text(
                'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding, role)'
                " VALUES (:uid, now(), 'late@example.test', 'oid-2', false, 'Judge')"
            ),
            {"uid": late_user_id},
        )

    # The new code, on 0004 alone, saves over the old pod's draft (no jurisdiction).
    from transcribe_api.domain import interface_dictation

    interface_dictation.engine, saved_engine = engine, interface_dictation.engine
    try:
        interface_dictation.upsert_live_draft(user_id, IAC_ID, {"saved": "by the new code"}, {})
    finally:
        interface_dictation.engine = saved_engine
    with engine.connect() as connection:
        drafts = connection.execute(text("SELECT jurisdiction_id::text, transcript FROM live_transcript_draft")).all()
    assert [tuple(d) for d in drafts] == [(IAC_ID, {"saved": "by the new code"})]

    migrate(command.upgrade, "0005")
    with engine.begin() as connection:
        membership = connection.execute(
            text("SELECT jurisdiction_id::text, user_id::text, role FROM jurisdiction_membership WHERE user_id = :uid"),
            {"uid": user_id},
        ).one()
        assert tuple(membership) == (IAC_ID, user_id, "judge")
        assert set(connection.execute(text("SELECT jurisdiction_id::text FROM transcription")).scalars()) == {IAC_ID}
        late = connection.execute(
            text("SELECT jurisdiction_id::text FROM jurisdiction_membership WHERE user_id = :uid"),
            {"uid": late_user_id},
        ).scalar_one()
        assert late == IAC_ID
        assert (
            connection.execute(text("SELECT jurisdiction_id::text FROM live_transcript_draft")).scalar_one() == IAC_ID
        )
        # A second jurisdiction's draft for the same user is allowed now.
        connection.execute(
            text(
                "INSERT INTO jurisdiction (id, code, name, enabled, auto_enrol, settings, created_datetime,"
                " updated_datetime) VALUES (gen_random_uuid(), 'ET', 'Employment Tribunal', true, false, '{}',"
                " now(), now())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO live_transcript_draft (id, created_datetime, user_id, jurisdiction_id, transcript,"
                " form_data) SELECT gen_random_uuid(), now(), :uid, id, '{}', '{}' FROM jurisdiction"
                " WHERE code = 'ET'"
            ),
            {"uid": user_id},
        )
        # 0005 runs as the first new pod starts; an old pod still serving makes a
        # transcription without a jurisdiction. It is the IAC's, not lost.
        connection.execute(
            text(
                "INSERT INTO transcription (id, created_datetime, user_id, title)"
                " VALUES (gen_random_uuid(), now(), :uid, 'E v F')"
            ),
            {"uid": user_id},
        )
        assert (
            connection.execute(
                text("SELECT jurisdiction_id::text FROM transcription WHERE title = 'E v F'")
            ).scalar_one()
            == IAC_ID
        )

    migrate(command.downgrade, "0003")
    with engine.connect() as connection:
        drafts = connection.execute(text("SELECT count(*) FROM live_transcript_draft")).scalar_one()
        titles = connection.execute(text("SELECT title FROM transcription")).scalars().all()
    assert drafts == 1  # the IAC draft; the ET one is gone
    assert sorted(titles) == ["A v B", "C v D", "E v F"]


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
def test_0004_and_0005_keep_every_existing_value_unchanged():
    """DIAAT-331: after 0004, and again after 0005, every row that existed before is
    still there with every value it had; only the new columns are added."""
    from sqlalchemy import create_engine, text

    from alembic import command

    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))

        def migrate(revision):
            with engine.begin() as connection:
                config = migrations.alembic_config()
                config.attributes["connection"] = connection
                command.upgrade(config, revision)

        migrate("0003")
        ids = {"u": "3f2a9c1e-8b7d-4e6f-9a1b-2c3d4e5f6a7b", "t": "0f8fad5b-d9cb-469f-a165-70867728950e"}
        with engine.begin() as connection:
            for statement in (
                'INSERT INTO "user" (id, created_datetime, updated_datetime, email, azure_user_id,'
                " has_completed_onboarding, role) VALUES (:u, '2026-01-02 03:04:05', '2026-01-03 03:04:05',"
                " 'judge@example.test', 'oid', true, 'Judge')",
                "INSERT INTO transcription (id, created_datetime, user_id, title)"
                " VALUES (:t, '2026-01-02 03:04:05', :u, 'A v B')",
                "INSERT INTO minuteversion (id, created_datetime, html_content, template, transcription_id,"
                " star_rating) VALUES (gen_random_uuid(), now(), '<p>Decision</p>', '{\"name\": \"General\"}', :t, 4)",
                "INSERT INTO transcriptionjob (id, created_datetime, transcription_id, dialogue_entries, s3_audio_url,"
                " provenance, needs_cleanup) VALUES (gen_random_uuid(), now(), :t,"
                ' \'[{"speaker": "Judge", "text": "words"}]\', \'user-uploads/oid/hearing-document-1.docx\','
                ' \'{"output": "document"}\', false)',
                "INSERT INTO transcription_job (id, created_datetime, user_id, status, audio_url, locale,"
                " enable_diarization, needs_cleanup, metadata, dialogue_entries) VALUES (gen_random_uuid(), now(), :u,"
                " 'SUCCEEDED', 'https://blob/a.wav', 'en-GB', true, false, '{\"case_reference\": \"PA/1/2026\"}', '[]')",
                "INSERT INTO live_transcript_draft (id, created_datetime, user_id, transcript, form_data)"
                ' VALUES (gen_random_uuid(), now(), :u, \'{"background": ["words"]}\', \'{"appellantName": "A"}\')',
                "INSERT INTO document_content (id, content, updated_at) VALUES (1, '{\"legal_frameworks\": [1]}', now())",
            ):
                connection.execute(text(statement), ids)

        # Whole rows, as JSON, without the columns 0004 adds.
        added = {"user": ["active_jurisdiction_id"]}
        tables = (
            "user",
            "transcription",
            "minuteversion",
            "transcriptionjob",
            "transcription_job",
            "live_transcript_draft",
            "document_content",
        )
        queries = {
            table: f"SELECT to_jsonb(t) - ARRAY{added.get(table, []) + ['jurisdiction_id']}::text[]"
            f' FROM "{table}" t ORDER BY 1::text'
            for table in tables
        }

        def snapshot():
            with engine.connect() as connection:
                return {name: connection.execute(text(query)).all() for name, query in queries.items()}

        before = snapshot()
        assert all(before.values()), before  # a row in every table, or the check proves nothing
        migrate("0004")
        assert snapshot() == before
        migrate("head")
        assert snapshot() == before
    finally:
        engine.dispose()
