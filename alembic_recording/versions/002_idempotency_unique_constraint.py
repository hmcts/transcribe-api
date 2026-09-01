"""Add composite UNIQUE constraint on (caller_id, idempotency_key).

index=True on idempotency_key created a non-unique index which allowed
concurrent POST /jobs requests with the same idempotency_key to both
pass the application-level check and insert duplicate rows.

The composite constraint scopes uniqueness per caller (two different
callers may legitimately reuse the same key string). NULL values are
excluded from uniqueness checks by PostgreSQL semantics, so jobs
without an idempotency_key are unaffected.

Revision ID: 002
Revises: 001
Create Date: 2026-05-19
"""

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the old non-unique index created by index=True on the column.
    op.drop_index(
        "ix_transcription_job_idempotency_key", table_name="transcription_job", if_exists=True
    )

    # Create the composite unique constraint (also creates an implicit unique index).
    op.create_unique_constraint(
        "uq_transcription_job_caller_idempotency",
        "transcription_job",
        ["caller_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_transcription_job_caller_idempotency", "transcription_job", type_="unique"
    )
    op.create_index(
        "ix_transcription_job_idempotency_key", "transcription_job", ["idempotency_key"]
    )
