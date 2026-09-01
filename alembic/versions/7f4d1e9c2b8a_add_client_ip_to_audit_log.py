"""Add client_ip column to audit_log table

Revision ID: 7f4d1e9c2b8a
Revises: 8e2a5d9f3b7c
Create Date: 2026-07-16 00:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "7f4d1e9c2b8a"
down_revision = "8e2a5d9f3b7c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("client_ip", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "client_ip")
