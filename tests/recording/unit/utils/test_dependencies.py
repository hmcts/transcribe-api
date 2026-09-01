"""Unit tests for _resolve_user() in utils/dependencies.py."""

from unittest.mock import MagicMock, patch

from sqlalchemy.exc import IntegrityError


def _resolve_user(azure_user_id="azure-123", email="test@example.com", roles=None):
    from transcribe_api.domain.auth.dependencies_recording import _resolve_user as _fn

    return _fn(azure_user_id, email, roles if roles is not None else ["Normal"])


class TestResolveUserCreatesNewUser:
    def _setup(self, roles=None):
        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = None
        return mock_session, roles or ["Normal"]

    def test_inserts_user_with_correct_fields(self):
        mock_session, roles = self._setup(roles=["Normal"])

        with (
            patch("transcribe_api.domain.auth.dependencies_recording.get_engine"),
            patch("transcribe_api.domain.auth.dependencies_recording.Session") as MockSession,
        ):
            MockSession.return_value.__enter__.return_value = mock_session
            _resolve_user(roles=roles)

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        user = mock_session.add.call_args[0][0]
        assert user.email == "test@example.com"
        assert user.azure_user_id == "azure-123"
        assert user.role == "Normal"

    def test_assigns_highest_priority_role_to_new_user(self):
        # When a user holds multiple roles, the most privileged one is stored.
        mock_session, _ = self._setup(roles=["Normal", "SystemAdministrator"])

        with (
            patch("transcribe_api.domain.auth.dependencies_recording.get_engine"),
            patch("transcribe_api.domain.auth.dependencies_recording.Session") as MockSession,
        ):
            MockSession.return_value.__enter__.return_value = mock_session
            _resolve_user(roles=["Normal", "SystemAdministrator"])

        user = mock_session.add.call_args[0][0]
        assert user.role == "SystemAdministrator"

    def test_returns_authenticated_user_with_app_roles(self):
        from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser

        mock_session, roles = self._setup(roles=["Normal"])

        with (
            patch("transcribe_api.domain.auth.dependencies_recording.get_engine"),
            patch("transcribe_api.domain.auth.dependencies_recording.Session") as MockSession,
        ):
            MockSession.return_value.__enter__.return_value = mock_session
            result = _resolve_user(roles=roles)

        assert isinstance(result, AuthenticatedUser)
        assert result.app_roles == ["Normal"]


class TestResolveUserRoleSync:
    def _existing_user(self, role="Normal"):
        from transcribe_api.domain.models_recording import User

        return User(email="test@example.com", azure_user_id="azure-123", role=role)

    def test_updates_role_when_app_roles_elevate_user(self):
        user = self._existing_user(role="Normal")
        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = user

        with (
            patch("transcribe_api.domain.auth.dependencies_recording.get_engine"),
            patch("transcribe_api.domain.auth.dependencies_recording.Session") as MockSession,
        ):
            MockSession.return_value.__enter__.return_value = mock_session
            _resolve_user(roles=["SystemAdministrator"])

        assert user.role == "SystemAdministrator"
        mock_session.commit.assert_called_once()

    def test_does_not_commit_when_role_is_unchanged(self):
        user = self._existing_user(role="Normal")
        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = user

        with (
            patch("transcribe_api.domain.auth.dependencies_recording.get_engine"),
            patch("transcribe_api.domain.auth.dependencies_recording.Session") as MockSession,
        ):
            MockSession.return_value.__enter__.return_value = mock_session
            _resolve_user(roles=["Normal"])

        assert user.role == "Normal"
        mock_session.commit.assert_not_called()

    def test_does_not_overwrite_role_when_jwt_carries_no_known_role(self):
        # primary_role resolves to None when no JWT role matches the configured
        # role set; the existing DB role must be left untouched.
        user = self._existing_user(role="Judge")
        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = user

        with (
            patch("transcribe_api.domain.auth.dependencies_recording.get_engine"),
            patch("transcribe_api.domain.auth.dependencies_recording.Session") as MockSession,
        ):
            MockSession.return_value.__enter__.return_value = mock_session
            _resolve_user(roles=[])

        assert user.role == "Judge"
        mock_session.commit.assert_not_called()


class TestResolveUserConcurrentCreate:
    def test_returns_winning_row_on_integrity_error(self):
        from transcribe_api.domain.models_recording import User

        winning_user = User(email="test@example.com", azure_user_id="azure-123", role="Normal")

        # First exec().first() sees no row; after rollback exec().one() returns
        # the row inserted by the concurrent winner.
        first_result = MagicMock()
        first_result.first.return_value = None
        second_result = MagicMock()
        second_result.one.return_value = winning_user

        mock_session = MagicMock()
        mock_session.exec.side_effect = [first_result, second_result]
        mock_session.commit.side_effect = IntegrityError("", {}, Exception())

        with (
            patch("transcribe_api.domain.auth.dependencies_recording.get_engine"),
            patch("transcribe_api.domain.auth.dependencies_recording.Session") as MockSession,
        ):
            MockSession.return_value.__enter__.return_value = mock_session
            result = _resolve_user(roles=["Normal"])

        mock_session.rollback.assert_called_once()
        assert result.db_user is winning_user

    def test_does_not_raise_on_concurrent_create(self):
        from transcribe_api.domain.models_recording import User

        winning_user = User(email="test@example.com", azure_user_id="azure-123", role="Normal")

        first_result = MagicMock()
        first_result.first.return_value = None
        second_result = MagicMock()
        second_result.one.return_value = winning_user

        mock_session = MagicMock()
        mock_session.exec.side_effect = [first_result, second_result]
        mock_session.commit.side_effect = IntegrityError("", {}, Exception())

        with (
            patch("transcribe_api.domain.auth.dependencies_recording.get_engine"),
            patch("transcribe_api.domain.auth.dependencies_recording.Session") as MockSession,
        ):
            MockSession.return_value.__enter__.return_value = mock_session

            # Must not propagate IntegrityError to the caller.
            result = _resolve_user(roles=["Normal"])

        assert result is not None
