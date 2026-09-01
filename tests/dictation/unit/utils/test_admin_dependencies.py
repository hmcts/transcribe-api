"""Unit tests for role-based authorization dependencies."""

import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

from transcribe_api.domain.models_dictation import User
from transcribe_api.domain.auth.approles_dictation import get_role


def _make_user(email: str, roles: list[str]) -> User:
    user = User(id="test-id", email=email, azure_user_id="oid-123")
    user.__dict__["app_roles"] = roles
    return user


def _make_request(method: str = "GET", path: str = "/transcriptions/abc") -> MagicMock:
    request = MagicMock()
    request.method = method
    request.url.path = path
    return request


# ---------------------------------------------------------------------------
# No role constraints — any authenticated user passes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_roles_required_passes_any_user(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    dep = get_allowlisted_user()
    result = await dep(request=_make_request(), current_user=_make_user("user@justice.gov.uk", []))
    assert result is not None


@pytest.mark.asyncio
async def test_no_roles_required_local_dev_passes(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=True)
    dep = get_allowlisted_user()
    result = await dep(request=_make_request(), current_user=_make_user("developer@localhost.com", []))
    assert result is not None


# ---------------------------------------------------------------------------
# required_roles_all — user must hold every listed role
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_roles_all_passes_when_user_has_all_roles(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    user = _make_user("user@justice.gov.uk", [get_role("SystemAdministrator"), get_role("LegalTextManager")])
    dep = get_allowlisted_user(required_roles_all=[get_role("SystemAdministrator"), get_role("LegalTextManager")])
    assert await dep(request=_make_request(), current_user=user) is user


@pytest.mark.asyncio
async def test_roles_all_denied_when_user_missing_one_role(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    user = _make_user("user@justice.gov.uk", [get_role("SystemAdministrator")])
    dep = get_allowlisted_user(required_roles_all=[get_role("SystemAdministrator"), get_role("LegalTextManager")])
    with pytest.raises(HTTPException) as exc_info:
        await dep(request=_make_request(), current_user=user)
    assert exc_info.value.status_code == 403
    assert get_role("LegalTextManager") in exc_info.value.detail


@pytest.mark.asyncio
async def test_roles_all_denied_when_user_has_no_roles(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    dep = get_allowlisted_user(required_roles_all=[get_role("SystemAdministrator")])
    with pytest.raises(HTTPException) as exc_info:
        await dep(request=_make_request(), current_user=_make_user("user@justice.gov.uk", []))
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# required_roles_any — user must hold at least one of the listed roles
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_roles_any_passes_when_user_has_one_matching_role(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    user = _make_user("user@justice.gov.uk", [get_role("LegalTextManager")])
    dep = get_allowlisted_user(required_roles_any=[get_role("SystemAdministrator"), get_role("LegalTextManager")])
    assert await dep(request=_make_request(), current_user=user) is user


@pytest.mark.asyncio
async def test_roles_any_denied_when_user_has_none_of_the_roles(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    user = _make_user("user@justice.gov.uk", [get_role("Judge")])
    dep = get_allowlisted_user(required_roles_any=[get_role("SystemAdministrator"), get_role("LegalTextManager")])
    with pytest.raises(HTTPException) as exc_info:
        await dep(request=_make_request(), current_user=user)
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Combined _all and _any
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combined_all_and_any_passes_when_both_satisfied(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    user = _make_user("user@justice.gov.uk", [get_role("SystemAdministrator"), get_role("LegalTextManager")])
    dep = get_allowlisted_user(
        required_roles_all=[get_role("SystemAdministrator")],
        required_roles_any=[get_role("LegalTextManager"), get_role("Judge")],
    )
    assert await dep(request=_make_request(), current_user=user) is user


@pytest.mark.asyncio
async def test_combined_fails_when_all_not_satisfied(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    user = _make_user("user@justice.gov.uk", [get_role("LegalTextManager")])
    dep = get_allowlisted_user(
        required_roles_all=[get_role("SystemAdministrator")],
        required_roles_any=[get_role("LegalTextManager")],
    )
    with pytest.raises(HTTPException):
        await dep(request=_make_request(), current_user=user)


# ---------------------------------------------------------------------------
# Local dev bypass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_dev_bypasses_all_role_checks(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=True)
    user = _make_user("developer@localhost.com", [])
    dep = get_allowlisted_user(
        required_roles_all=[get_role("SystemAdministrator")],
        required_roles_any=[get_role("LegalTextManager")],
    )
    assert await dep(request=_make_request(), current_user=user) is user


# ---------------------------------------------------------------------------
# UNAUTHORISED_ACCESS_ATTEMPT — structured log on 403
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthorised_access_attempt_logged_on_roles_any_failure(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    mock_logger = mocker.patch("hmcts_azure_auth.dependencies.logger")
    user = _make_user("judge@justice.gov.uk", [get_role("Judge")])
    dep = get_allowlisted_user(required_roles_any=[get_role("SystemAdministrator")])
    with pytest.raises(HTTPException):
        await dep(request=_make_request("GET", "/admin/transcriptions"), current_user=user)
    log_call = mock_logger.warning.call_args[0][0]
    assert "UNAUTHORISED_ACCESS_ATTEMPT" in log_call


@pytest.mark.asyncio
async def test_unauthorised_access_attempt_logged_on_roles_all_failure(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    mock_logger = mocker.patch("hmcts_azure_auth.dependencies.logger")
    user = _make_user("judge@justice.gov.uk", [get_role("Judge")])
    dep = get_allowlisted_user(required_roles_all=[get_role("SystemAdministrator")])
    with pytest.raises(HTTPException):
        await dep(request=_make_request("DELETE", "/transcriptions/abc"), current_user=user)
    log_call = mock_logger.warning.call_args[0][0]
    assert "UNAUTHORISED_ACCESS_ATTEMPT" in log_call


@pytest.mark.asyncio
async def test_unauthorised_access_attempt_log_contains_required_fields(mocker):
    from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user
    mocker.patch("hmcts_azure_auth.dependencies._is_local_dev", return_value=False)
    mock_logger = mocker.patch("hmcts_azure_auth.dependencies.logger")
    user = _make_user("judge@justice.gov.uk", [get_role("Judge")])
    dep = get_allowlisted_user(required_roles_any=[get_role("SystemAdministrator")])
    with pytest.raises(HTTPException):
        await dep(request=_make_request("GET", "/admin/transcriptions"), current_user=user)
    args = mock_logger.warning.call_args[0]
    # args[0] is format string, args[1..] are the values: user_id, user_email, held_roles, required_roles, attempted_resource
    assert args[1] == "test-id"                          # user_id
    assert args[2] == "judge@justice.gov.uk"             # user_email
    assert get_role("Judge") in args[3]                  # held_roles
    assert get_role("SystemAdministrator") in args[4]    # required_roles
    assert "GET" in args[5] and "/admin/transcriptions" in args[5]  # attempted_resource
