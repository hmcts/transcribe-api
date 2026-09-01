"""empty message

Revision ID: 5eb1a42de64d
Revises: a8f2c9d5e1b3, add_tags_table
Create Date: 2025-12-04 14:04:26.522967

"""

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision = "5eb1a42de64d"
down_revision = ("a8f2c9d5e1b3", "add_tags_table")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
