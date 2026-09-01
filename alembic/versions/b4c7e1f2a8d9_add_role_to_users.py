"""Add role column to users

Revision ID: b4c7e1f2a8d9
Revises: 5eb1a42de64d
Create Date: 2026-03-10 12:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b4c7e1f2a8d9"
down_revision = "5eb1a42de64d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user", sa.Column("role", sa.String(), nullable=True))
    op.execute("UPDATE \"user\" SET role = 'user' WHERE role IS NULL")
    op.alter_column("user", "role", nullable=False, server_default="user")
    op.create_index(op.f("ix_user_role"), "user", ["role"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_user_role"), table_name="user")
    op.drop_column("user", "role")
