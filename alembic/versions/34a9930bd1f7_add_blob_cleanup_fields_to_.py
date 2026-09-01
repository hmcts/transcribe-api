"""Add blob cleanup fields to TranscriptionJob

Revision ID: 34a9930bd1f7
Revises: ed7aa9a4e0f2
Create Date: 2025-10-07 14:32:24.396510

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "34a9930bd1f7"
down_revision = "ed7aa9a4e0f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add blob deletion cleanup fields to TranscriptionJob
    op.add_column("transcriptionjob", sa.Column("needs_cleanup", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("transcriptionjob", sa.Column("cleanup_failure_reason", sa.String(), nullable=True))


def downgrade() -> None:
    # Remove blob deletion cleanup fields from TranscriptionJob
    op.drop_column("transcriptionjob", "cleanup_failure_reason")
    op.drop_column("transcriptionjob", "needs_cleanup")
