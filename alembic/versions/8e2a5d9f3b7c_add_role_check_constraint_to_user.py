"""Add CHECK constraint on user.role and normalise legacy 'user' values

Revision ID: 8e2a5d9f3b7c
Revises: 3c8a2f94b6d1
Create Date: 2026-07-16 00:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "8e2a5d9f3b7c"
down_revision = "3c8a2f94b6d1"
branch_labels = None
depends_on = None

_VALID_ROLES = ("Normal", "Judge", "LegalTextManager", "SystemAdministrator")
_CONSTRAINT_NAME = "ck_user_role_valid"


def upgrade() -> None:
    # Normalise any rows carrying the legacy "user" default to "Normal"
    # before applying the constraint so we don't violate it on existing data.
    op.execute("UPDATE \"user\" SET role = 'Normal' WHERE role = 'user'")

    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "user",
        sa.text(f"role IN {_VALID_ROLES}"),
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "user", type_="check")
