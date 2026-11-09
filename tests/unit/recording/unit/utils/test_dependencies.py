"""Unit tests for _resolve_user() in domain/auth/dependencies_recording.py.

Since DIAAT-373 it loads the user a BenchNotes token names; it never creates one (sign-in does,
domain/auth/sessions.py: tests/unit/test_first_login_user_race.py) and never writes User.role.
"""

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from transcribe_api.domain.auth.app_token import AppPrincipal, Context

MODULE = "transcribe_api.domain.auth.dependencies_recording"


def _principal(ctx=Context("IAC", "Judge"), sysadmin=False, user_id=None):
    return AppPrincipal(
        user_id=user_id or uuid4(),
        sysadmin=sysadmin,
        assignments=(ctx,) if ctx else (),
        ctx=ctx,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )


def _resolve_user(principal=None):
    from transcribe_api.domain.auth.dependencies_recording import _resolve_user as _fn

    return _fn(principal or _principal())


def _existing_user(role=None, is_active=True):
    from transcribe_api.domain.models_recording import User

    return User(email="test@example.com", azure_user_id="azure-123", role=role, is_active=is_active)


def _session_returning(user):
    mock_session = MagicMock()
    mock_session.get.return_value = user
    return mock_session


def _resolve(user, principal=None):
    mock_session = _session_returning(user)
    with patch(f"{MODULE}.get_engine"), patch(f"{MODULE}.Session") as MockSession:
        MockSession.return_value.__enter__.return_value = mock_session
        return _resolve_user(principal), mock_session


class TestResolveUserLoadsTheTokensUser:
    def test_returns_the_user_with_the_context_the_token_carries(self):
        from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser

        user = _existing_user()
        principal = _principal(Context("ET", "LegalTextManager"), sysadmin=True)

        result, mock_session = _resolve(user, principal)

        assert isinstance(result, AuthenticatedUser)
        assert result.db_user is user
        assert result.ctx == Context("ET", "LegalTextManager") and result.sysadmin is True
        assert result.assignments == (Context("ET", "LegalTextManager"),)
        mock_session.get.assert_called_once()

    def test_the_permissions_are_those_of_the_active_context_only(self):
        from transcribe_api.domain.auth.authorisation import permissions_of

        principal = _principal(Context("IAC", "Judge"))
        principal = AppPrincipal(
            **{**principal.__dict__, "assignments": (Context("IAC", "Judge"), Context("ET", "JurisdictionAdministrator"))}
        )
        result, _ = _resolve(_existing_user(), principal)
        assert "dictation.use" in permissions_of(result) and "users.manage" not in permissions_of(result)

    def test_a_system_administrator_with_no_context_holds_only_the_system_permissions(self):
        from transcribe_api.domain.auth.authorisation import permissions_of
        from transcribe_api.domain.auth.permissions import SYSTEM_ADMIN_PERMISSIONS

        result, _ = _resolve(_existing_user(), _principal(ctx=None, sysadmin=True))
        assert set(permissions_of(result)) == SYSTEM_ADMIN_PERMISSIONS and result.ctx is None

    def test_no_context_and_no_sysadmin_holds_no_permission_at_all(self):
        from transcribe_api.domain.auth.authorisation import permissions_of

        result, _ = _resolve(_existing_user(), _principal(ctx=None))
        assert permissions_of(result) == []


class TestResolveUserNeverWritesOrCreates:
    @pytest.mark.parametrize("role", [None, "Judge", "LegalTextManager"])
    def test_user_role_is_not_overwritten_from_the_token(self, role):
        user = _existing_user(role=role)
        _, mock_session = _resolve(user, _principal(sysadmin=True))
        assert user.role == role
        mock_session.add.assert_not_called()
        mock_session.commit.assert_not_called()

    def test_a_user_the_database_lacks_is_refused_not_created(self):
        mock_session = _session_returning(None)
        with patch(f"{MODULE}.get_engine"), patch(f"{MODULE}.Session") as MockSession:
            MockSession.return_value.__enter__.return_value = mock_session
            with pytest.raises(HTTPException) as refused:
                _resolve_user()
        assert refused.value.status_code == 401
        mock_session.add.assert_not_called()
        mock_session.commit.assert_not_called()

    def test_a_deactivated_user_is_refused_and_not_reenabled(self):
        user = _existing_user(is_active=False)
        with pytest.raises(HTTPException) as refused:
            _resolve(user)
        assert refused.value.status_code == 403 and refused.value.detail["code"] == "user_deactivated"
        assert user.is_active is False

    def test_does_not_log_the_email_of_the_user(self, caplog):
        with caplog.at_level(logging.DEBUG):
            _resolve(_existing_user())
        assert "test@example.com" not in caplog.text
