"""The role -> permission map (DIAAT-373, DIAAT-376; domain/auth/permissions.py)."""

from __future__ import annotations

import pytest

from transcribe_api.domain.auth import permissions as p

# The contract's list, exactly (authz-contract.md section 1).
CONTRACT_PERMISSIONS = ["app.access", "context.switch", "access.request", "access.approve", "dictation.use", "recording.upload", "recording.own", "documents.export", "recording.audit.read", "recording.any.read", "recording.any.edit", "recording.any.delete", "dictation.any.read", "admin.access", "legal_text.manage", "templates.manage", "document_content.read", "document_content.write", "users.manage", "jurisdiction_admin.assign", "jurisdiction_super_admin.assign", "role_audit.read", "jurisdiction.create", "jurisdictions.manage", "jurisdiction.wording.edit", "jurisdiction.features", "feature_flags.manage", "system.settings", "security.alert_test"]

COMMON = ["app.access", "context.switch", "access.request"]

CONTRACT_ROLE_MAP = {
    "Judge": COMMON
    + ["dictation.use", "recording.upload", "recording.own", "documents.export", "recording.audit.read"]
    + ["document_content.read"],
    "LegalTextManager": COMMON
    + ["admin.access", "legal_text.manage", "templates.manage", "document_content.read", "document_content.write"],
    "JurisdictionAdministrator": COMMON
    + [
        "admin.access",
        "users.manage",
        "access.approve",
        "role_audit.read",
        "templates.manage",
        "jurisdictions.manage",
        "jurisdiction.wording.edit",
        "jurisdiction.features",
    ],
}
CONTRACT_ROLE_MAP["JurisdictionSuperAdministrator"] = CONTRACT_ROLE_MAP["JurisdictionAdministrator"] + [
    "jurisdiction.create",
    "jurisdiction_admin.assign",
]
CONTRACT_SYSADMIN = [
    "admin.access",
    "feature_flags.manage",
    "system.settings",
    "security.alert_test",
    "jurisdiction_super_admin.assign",
]


def test_the_permission_list_is_the_contracts():
    assert list(p.PERMISSIONS) == CONTRACT_PERMISSIONS
    assert len(set(p.PERMISSIONS)) == len(p.PERMISSIONS)


def test_the_stored_roles_are_the_four_and_never_system_administrator_or_normal():
    assert p.STORED_ROLES == (
        "Judge",
        "LegalTextManager",
        "JurisdictionAdministrator",
        "JurisdictionSuperAdministrator",
    )
    assert "SystemAdministrator" not in p.STORED_ROLES
    assert "Normal" not in p.STORED_ROLES
    assert set(p.ROLE_PERMISSIONS) == set(p.STORED_ROLES)


@pytest.mark.parametrize("role", p.STORED_ROLES)
def test_each_role_has_exactly_the_contracts_permissions(role):
    assert set(p.permissions_for(role)) == set(CONTRACT_ROLE_MAP[role])


def test_only_a_judge_has_the_recording_and_dictation_permissions():
    own = {"dictation.use", "recording.upload", "recording.own", "documents.export", "recording.audit.read"}
    for role in p.STORED_ROLES:
        for sysadmin in (False, True):
            held = set(p.permissions_for(role, sysadmin))
            assert (own <= held) is (role == "Judge"), (role, sysadmin)
            assert not held & (own - {"recording.audit.read"}) or role == "Judge"


def test_nobody_has_the_any_permissions():
    anyone = {"recording.any.read", "recording.any.edit", "recording.any.delete", "dictation.any.read"}
    assert anyone == set(p.UNGRANTED_PERMISSIONS)
    for role in (*p.STORED_ROLES, None):
        for sysadmin in (False, True):
            assert not anyone & set(p.permissions_for(role, sysadmin))


def test_the_system_permissions_apply_only_while_acting_as_system_administrator():
    assert set(p.permissions_for("SystemAdministrator", True)) == set(CONTRACT_SYSADMIN)
    for role in p.STORED_ROLES:
        assert set(p.permissions_for(role, True)) == set(CONTRACT_ROLE_MAP[role])
    assert p.permissions_for(None, True) == []
    assert p.permissions_for(None, False) == []


def test_acting_as_system_administrator_needs_the_entra_role():
    assert p.permissions_for("SystemAdministrator", False) == []


def test_a_system_administrator_gets_no_recording_or_dictation_permission():
    held = set(p.permissions_for("SystemAdministrator", True))
    assert not held & {"dictation.use", "recording.own", "recording.upload", "documents.export", "app.access"}


def test_only_a_super_administrator_creates_jurisdictions_and_only_system_administrators_assign_them():
    for role in p.STORED_ROLES:
        for sysadmin in (False, True):
            held = set(p.permissions_for(role, sysadmin))
            assert ("jurisdiction.create" in held) is (role == "JurisdictionSuperAdministrator")
            assert "jurisdiction_super_admin.assign" not in held
            assert ("jurisdiction_admin.assign" in held) is (role == "JurisdictionSuperAdministrator")
    assert "jurisdiction_super_admin.assign" in p.permissions_for("SystemAdministrator", True)


def test_a_role_that_is_not_a_context_role_has_no_permissions_with_or_without_the_flag():
    for role in ("Normal", "Admin", "judge", "systemadministrator", ""):
        for sysadmin in (False, True):
            assert p.permissions_for(role, sysadmin) == []


def test_system_administrator_is_a_context_role_but_never_a_stored_one():
    assert p.CONTEXT_ROLES == (*p.STORED_ROLES, "SystemAdministrator")


def test_permissions_come_in_the_order_of_the_list():
    for role in ("JurisdictionSuperAdministrator", "SystemAdministrator"):
        held = p.permissions_for(role, True)
        assert held == [permission for permission in p.PERMISSIONS if permission in held]


def test_every_role_map_entry_is_a_listed_permission():
    listed = set(p.PERMISSIONS)
    for role, granted in p.ROLE_PERMISSIONS.items():
        assert granted <= listed, role
    assert p.SYSTEM_ADMIN_PERMISSIONS <= listed


def test_a_role_needs_a_jurisdiction_unless_it_is_global():
    assert [p.role_needs_jurisdiction(role) for role in p.STORED_ROLES] == [True, True, True, False]
    assert not p.role_needs_jurisdiction("SystemAdministrator")


def test_only_judge_and_legal_text_manager_can_be_requested():
    assert p.REQUESTABLE_ROLES == ("Judge", "LegalTextManager")
