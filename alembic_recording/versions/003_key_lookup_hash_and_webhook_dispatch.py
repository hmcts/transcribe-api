"""Add key_lookup_hash to caller and webhook_dispatched_at to transcription_job.

key_lookup_hash: SHA-256 of the raw API key stored alongside the bcrypt hash.
  Enables O(1) indexed lookup before bcrypt verification instead of scanning
  all active callers. NULL for legacy rows; auth falls back to linear scan.

webhook_dispatched_at: Timestamp set atomically by the first replica to
  dispatch a webhook for a completed job. Prevents duplicate delivery when
  multiple replicas process the same job (SELECT FOR UPDATE lock is released
  before processing completes).

Revision ID: 003
Revises: 002
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "caller",
        sa.Column("key_lookup_hash", sa.String(), nullable=True),
    )
    op.create_index("ix_caller_key_lookup_hash", "caller", ["key_lookup_hash"])

    op.add_column(
        "transcription_job",
        sa.Column("webhook_dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcription_job", "webhook_dispatched_at")
    op.drop_index("ix_caller_key_lookup_hash", table_name="caller")
    op.drop_column("caller", "key_lookup_hash")
