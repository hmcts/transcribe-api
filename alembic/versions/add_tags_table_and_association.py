"""Add tags table and association

Revision ID: add_tags_table
Revises: 34a9930bd1f7
Create Date: 2025-01-27 12:00:00.000000

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "add_tags_table"
down_revision = "34a9930bd1f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create tag table
    op.create_table(
        "tag",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_datetime", sa.DateTime(), nullable=False),
        sa.Column("updated_datetime", sa.DateTime(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tag_name"), "tag", ["name"], unique=True)

    # Create association table
    op.create_table(
        "transcription_tag_association",
        sa.Column("transcription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["transcription_id"],
            ["transcription.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tag.id"],
        ),
        sa.PrimaryKeyConstraint("transcription_id", "tag_id"),
    )


def downgrade() -> None:
    # Drop association table
    op.drop_table("transcription_tag_association")
    # Drop tag table
    op.drop_index(op.f("ix_tag_name"), table_name="tag")
    op.drop_table("tag")
