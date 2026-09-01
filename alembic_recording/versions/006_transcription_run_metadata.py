"""Add transcription run metadata columns to transcription_job.

transcription_duration_seconds: how long the transcription itself took to
  produce (Azure's own processing window when available, falling back to
  wall-clock time from job submission to completion).

model_identifier: which model/engine produced the transcript — Azure's
  model resource URL when returned by the batch API, otherwise a
  locale-qualified fallback.

audio_duration_seconds already existed prior to this migration (added in
001_initial_schema) and is not touched here.

Revision ID: 006
Revises: 005
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transcription_job",
        sa.Column("transcription_duration_seconds", sa.Float(), nullable=True),
    )
    op.add_column(
        "transcription_job",
        sa.Column("model_identifier", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcription_job", "model_identifier")
    op.drop_column("transcription_job", "transcription_duration_seconds")
