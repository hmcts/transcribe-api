"""A JurisdictionAdministrator's Judge and LegalTextManager roles become rows of their own (DIAAT-406).

Until now a JurisdictionAdministrator was also offered Judge and LegalTextManager in its jurisdiction
without holding them: derived when assignments were read. From DIAAT-406 every role a user acts in is one
they were given, and a JurisdictionAdministrator gives or takes Judge and LegalTextManager (to others or
itself) in the Users screen. So that nobody loses what they could do the day this is deployed, each
JurisdictionAdministrator assignment (of a user, or of an email not yet claimed) gets a Judge and a
LegalTextManager row in the same jurisdiction, unless it has one already. ``granted_by`` is NULL, as for
the other rows a migration made. A JurisdictionAdministrator who does not want them removes them in Users.

Only ``role_assignment`` is written. The previous release reads the new rows as what it already derived
(it skips a derived role the user also holds as a row), so it keeps working during the rollout.

Downgrade removes nothing: the rows made here cannot be told apart from ones given by hand since.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-12
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

INCLUDED_ROLES = ("Judge", "LegalTextManager")

MATERIALISE = """
INSERT INTO role_assignment (id, user_id, pending_email, jurisdiction_id, role, granted_by, granted_at, tester)
SELECT gen_random_uuid(), admin.user_id, admin.pending_email, admin.jurisdiction_id, included.role, NULL, now(), false
FROM role_assignment AS admin
CROSS JOIN (VALUES {roles}) AS included(role)
WHERE admin.role = 'JurisdictionAdministrator'
  AND admin.jurisdiction_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM role_assignment AS held
    WHERE held.jurisdiction_id = admin.jurisdiction_id
      AND held.role = included.role
      AND (
        (admin.user_id IS NOT NULL AND held.user_id = admin.user_id)
        OR (admin.user_id IS NULL AND held.user_id IS NULL AND held.pending_email = admin.pending_email)
      )
  )
"""


def upgrade() -> None:
    roles = ", ".join(f"('{role}')" for role in INCLUDED_ROLES)
    op.execute(sa.text(MATERIALISE.format(roles=roles)))


def downgrade() -> None:
    pass
