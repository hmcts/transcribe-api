"""Who of a jurisdiction's members sees it at each stage (DIAAT-345).

A new jurisdiction is set up as a draft, tried by its test group, then switched on for every
member (``jurisdiction.stage``). Its administrators see it from the start, to set it up. The
test group are the members marked ``tester`` (``role_assignment.tester``). ``enabled`` still
switches a jurisdiction off for everyone, whatever its stage. Kept apart from
domain/jurisdictions.py and domain/assignments.py so that both can use it.
"""

from __future__ import annotations

STAGE_DRAFT = "draft"
STAGE_TEST = "test"
STAGE_LIVE = "live"
STAGES = (STAGE_DRAFT, STAGE_TEST, STAGE_LIVE)

# A member who administers the jurisdiction, in the two spellings in use: the member role of
# the command line and admin area ("admin"), and the stored role (JurisdictionAdministrator).
_ADMIN_ROLES = frozenset({"admin", "JurisdictionAdministrator"})


def sees(stage: str, role: str, tester: bool) -> bool:
    """Whether a member with ``role`` (and in the test group or not) sees a jurisdiction
    at ``stage``. Its admins see it from the start, to set it up."""
    if stage == STAGE_LIVE:
        return True
    if stage == STAGE_TEST:
        return tester or role in _ADMIN_ROLES
    return stage == STAGE_DRAFT and role in _ADMIN_ROLES


def sees_template(stage: str, in_test_group: bool) -> bool:
    """Whether a member sees a template at ``stage`` on the recording page (DIAAT-347):
    live, everyone; test, the jurisdiction's test group (its testers and administrators).
    A draft is seen by no one there; the admin area previews it (DIAAT-346)."""
    if stage == STAGE_LIVE:
        return True
    return stage == STAGE_TEST and in_test_group
