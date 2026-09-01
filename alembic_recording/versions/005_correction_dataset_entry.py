"""Add correction_dataset_entry table (DIAAT-231).

Accumulates clerk corrections (whole-segment and word-range) into a durable
store separate from transcription_job.dialogue_entries, so the corpus can
later be exported to fine-tune/evaluate transcription models.

Writes are gated behind Settings.CORRECTIONS_DATASET_EXPORT_ENABLED, which
defaults to False: retention and anonymisation policy for this table (it can
hold real court-hearing content) has NOT yet been signed off by
legal/compliance — see CorrectionDatasetEntry's docstring in
transcription_svc/database/models.py. This migration only creates the
(empty) table; no real content is captured until that sign-off happens and
the flag is deliberately enabled in an environment.

Revision ID: 005
Revises: 004
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "correction_dataset_entry",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("transcription_job.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "caller_id", UUID(as_uuid=True), sa.ForeignKey("caller.id"), nullable=False, index=True
        ),
        sa.Column("segment_index", sa.Integer, nullable=False),
        sa.Column("correction_kind", sa.String, nullable=False),
        sa.Column("start_word_index", sa.Integer, nullable=True),
        sa.Column("end_word_index", sa.Integer, nullable=True),
        sa.Column("speaker", sa.String, nullable=False),
        sa.Column("locale", sa.String, nullable=False),
        sa.Column("original_text", sa.Text, nullable=False),
        sa.Column("corrected_text", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("correction_dataset_entry")
