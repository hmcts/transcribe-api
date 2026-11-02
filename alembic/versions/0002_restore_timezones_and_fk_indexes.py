"""Restore the timestamptz columns and the dictation foreign-key indexes.

The 0001 baseline was generated from models that did not declare what the old
migrations had created (docs/data-migration/schema-mapping.md):

- Every column that was `timestamp with time zone` in the old schemas came out
  as `timestamp without time zone`: all recording tables, and dictation's
  audit_log, live_transcript_draft and document_content. The app writes
  datetime.now(UTC), so naive values written since 0001 hold UTC wall-clock
  time (the server's TimeZone is UTC); `AT TIME ZONE 'UTC'` reads them as such
  whatever the migrating session's TimeZone is. Columns that were naive in the
  old schemas (user and the other dictation tables) stay naive.
- The three foreign-key indexes from dictation migration a8f2c9d5e1b3, the fix
  for 15-20s p95 latency on the transcription list endpoints, were missing.

0001 may already be applied, so this is a new revision rather than an edit.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# (table, column, nullable)
TIMESTAMPTZ_COLUMNS = [
    ("audit_log", "timestamp", False),
    ("caller", "created_datetime", False),
    ("caller", "updated_datetime", True),
    ("correction_dataset_entry", "created_datetime", False),
    ("correction_dataset_entry", "updated_datetime", True),
    ("document_content", "updated_at", False),
    ("live_transcript_draft", "created_datetime", False),
    ("live_transcript_draft", "updated_datetime", True),
    ("transcription_job", "created_datetime", False),
    ("transcription_job", "updated_datetime", True),
    ("transcription_job", "webhook_dispatched_at", True),
]

FK_INDEXES = [
    ("ix_transcription_user_id", "transcription", "user_id"),
    ("ix_minuteversion_transcription_id", "minuteversion", "transcription_id"),
    ("ix_transcriptionjob_transcription_id", "transcriptionjob", "transcription_id"),
]


def _set_timezone(table: str, column: str, nullable: bool, timezone: bool) -> None:
    # timestamp AT TIME ZONE 'UTC' -> timestamptz, and timestamptz AT TIME ZONE
    # 'UTC' -> timestamp: either way the instant is kept, read as UTC.
    op.alter_column(
        table,
        column,
        type_=sa.DateTime(timezone=timezone),
        existing_type=sa.DateTime(timezone=not timezone),
        existing_nullable=nullable,
        postgresql_using=f"\"{column}\" AT TIME ZONE 'UTC'",
    )


def upgrade() -> None:
    for name, table, column in FK_INDEXES:
        op.create_index(name, table, [column], unique=False)
    for table, column, nullable in TIMESTAMPTZ_COLUMNS:
        _set_timezone(table, column, nullable, timezone=True)


def downgrade() -> None:
    for table, column, nullable in TIMESTAMPTZ_COLUMNS:
        _set_timezone(table, column, nullable, timezone=False)
    for name, table, _column in reversed(FK_INDEXES):
        op.drop_index(name, table_name=table)
