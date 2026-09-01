"""Add audit_log table

Revision ID: 3c8a2f94b6d1
Revises: c2d4f6a8b0e1
Create Date: 2026-07-16 00:00:00.000000

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "3c8a2f94b6d1"
down_revision = "c2d4f6a8b0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("held_roles", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("required_roles", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resource", sa.String(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_event_type"), "audit_log", ["event_type"], unique=False)
    op.create_index(op.f("ix_audit_log_timestamp"), "audit_log", ["timestamp"], unique=False)
    op.create_index(op.f("ix_audit_log_user_id"), "audit_log", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_log_user_id"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_timestamp"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_event_type"), table_name="audit_log")
    op.drop_table("audit_log")
