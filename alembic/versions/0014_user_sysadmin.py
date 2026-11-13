"""user.sysadmin: whether the user held the Entra SystemAdministrator role when they last signed in (DIAAT-407).

SystemAdministrator is still only an Entra role: it is copied into the BenchNotes token at sign-in and
grants nothing anywhere else. This column records the claim of the latest sign-in so that a request made
by someone else can tell that an account holds it: nobody deactivates or reactivates such an account
(domain/user_admin.py). Every sign-in sets it, true or false.

Users who signed in before this release are seeded from the retired ``user.role``, which pre-0009 sign-ins
set from the same claim; the rest are false until they next sign in.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-13
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user", sa.Column("sysadmin", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE \"user\" SET sysadmin = true WHERE role = 'SystemAdministrator'")


def downgrade() -> None:
    op.drop_column("user", "sysadmin")
