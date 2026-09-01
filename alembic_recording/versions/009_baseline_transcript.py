"""Add baseline_transcript to transcription_job.

baseline_transcript: A clerk-supplied reference transcript (e.g. a court
  reporter's transcript), stored as plain text against the job. Lets a real
  word error rate be computed against the whole auto-generated
  transcription, independent of any corrections made in this app (see
  audio/accuracy.py and audio/wer.py:baseline_word_error_rate).

Revision ID: 009
Revises: 008
Create Date: 2026-07-15
"""

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transcription_job",
        sa.Column("baseline_transcript", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcription_job", "baseline_transcript")
