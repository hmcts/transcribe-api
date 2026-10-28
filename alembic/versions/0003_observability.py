"""Observability columns: audit outcome and object, job lifecycle and provenance (DIAAT-205).

The DIAAT-205 schema from the two heritage services, merged into the combined
database (docs/data-migration/schema-mapping.md, "DIAAT-205 additions"):

- audit_log (dictation 9d1e6b3a4c2f, DIAAT-286): the outcome, object and request ID
  of an audited action, and email made optional. App rows identify the actor by
  user_id (the internal user UUID) only; the auth library's rows keep the email.
  The recording service's own audit table (recording 016) is not created: it writes
  these rows too.
- transcription_job (recording 014, 015, 017): trace_parent links a job's later spans
  to the request that created it (DIAAT-276); processing_started_datetime separates
  queue wait from processing (DIAAT-281); stuck_reported_at makes job.stuck fire
  once per job (DIAAT-281); provenance, the configuration that produced the
  transcript (DIAAT-286).
- transcriptionjob (dictation 9d1e6b3a4c2f, DIAAT-286): provenance.

All new columns are nullable with no default, so existing rows are untouched and
the upgrade takes only brief locks.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("outcome", sa.String(length=16), nullable=True))
    op.add_column("audit_log", sa.Column("object_type", sa.String(length=32), nullable=True))
    op.add_column("audit_log", sa.Column("object_id", sa.String(length=64), nullable=True))
    op.add_column("audit_log", sa.Column("request_id", sa.String(length=64), nullable=True))
    op.create_index("ix_audit_log_object_id", "audit_log", ["object_id"])
    op.create_index("ix_audit_log_request_id", "audit_log", ["request_id"])
    op.alter_column("audit_log", "email", existing_type=sa.String(), nullable=True)

    op.add_column("transcription_job", sa.Column("trace_parent", sa.String(length=64), nullable=True))
    op.add_column(
        "transcription_job", sa.Column("processing_started_datetime", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("transcription_job", sa.Column("stuck_reported_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("transcription_job", sa.Column("provenance", postgresql.JSONB(), nullable=True))

    op.add_column("transcriptionjob", sa.Column("provenance", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    # The new columns' values are lost. Rows written without an email get an empty
    # one, as the column is NOT NULL again.
    op.drop_column("transcriptionjob", "provenance")

    op.drop_column("transcription_job", "provenance")
    op.drop_column("transcription_job", "stuck_reported_at")
    op.drop_column("transcription_job", "processing_started_datetime")
    op.drop_column("transcription_job", "trace_parent")

    op.execute("UPDATE audit_log SET email = '' WHERE email IS NULL")
    op.alter_column("audit_log", "email", existing_type=sa.String(), nullable=False)
    op.drop_index("ix_audit_log_request_id", table_name="audit_log")
    op.drop_index("ix_audit_log_object_id", table_name="audit_log")
    op.drop_column("audit_log", "request_id")
    op.drop_column("audit_log", "object_id")
    op.drop_column("audit_log", "object_type")
    op.drop_column("audit_log", "outcome")
