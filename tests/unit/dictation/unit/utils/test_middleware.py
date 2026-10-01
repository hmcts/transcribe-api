import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request
from fastapi.responses import Response

from transcribe_api.runtime.middleware import add_request_id, request_id_ctx


class TestAddRequestId:
    """Test cases for the add_request_id middleware function."""

    @pytest.mark.asyncio
    async def test_adds_request_id_when_not_present(self):
        """Test that a new request ID is generated when not present in headers."""
        # Arrange
        request = Mock(spec=Request)
        request.headers = {}

        call_next = AsyncMock()
        call_next.return_value = Response()

        # Act
        response = await add_request_id(request, call_next)

        # Assert
        call_next.assert_called_once_with(request)
        assert "X-Request-Id" in response.headers, "X-Request-Id header should be added to response"
        assert response.headers["X-Request-Id"] is not None, "Request ID should not be None"
        assert len(response.headers["X-Request-Id"]) == 36, (
            f"Request ID should be 36 characters (UUID length), got {len(response.headers['X-Request-Id'])}"
        )

        # Verify context variable was set
        context_value = request_id_ctx.get()
        assert context_value == response.headers["X-Request-Id"], "Context variable should match response header"

    @pytest.mark.asyncio
    async def test_uses_existing_request_id_from_headers(self):
        """Test that existing request ID from headers is used."""
        # Arrange
        existing_id = "existing-request-id-123"
        request = Mock(spec=Request)
        request.headers = {"X-Request-Id": existing_id}

        call_next = AsyncMock()
        call_next.return_value = Response()

        # Act
        response = await add_request_id(request, call_next)

        # Assert
        assert response.headers["X-Request-Id"] == existing_id, (
            f"Should use existing request ID '{existing_id}', got '{response.headers['X-Request-Id']}'"
        )

        # Verify context variable was set
        context_value = request_id_ctx.get()
        assert context_value == existing_id, "Context variable should match existing request ID"

    @pytest.mark.asyncio
    async def test_preserves_existing_response_headers(self):
        """Test that existing response headers are preserved."""
        # Arrange
        request = Mock(spec=Request)
        request.headers = {}

        response = Response()
        response.headers["Content-Type"] = "application/json"
        response.headers["Cache-Control"] = "no-cache"

        call_next = AsyncMock()
        call_next.return_value = response

        # Act
        result = await add_request_id(request, call_next)

        # Assert
        assert result.headers["Content-Type"] == "application/json", "Content-Type header should be preserved"
        assert result.headers["Cache-Control"] == "no-cache", "Cache-Control header should be preserved"
        assert "X-Request-Id" in result.headers, "X-Request-Id header should be added"

    @pytest.mark.asyncio
    async def test_generates_valid_uuid_format(self):
        """Test that generated request IDs are valid UUIDs."""
        # Arrange
        request = Mock(spec=Request)
        request.headers = {}

        call_next = AsyncMock()
        call_next.return_value = Response()

        # Act
        response = await add_request_id(request, call_next)

        # Assert
        request_id = response.headers["X-Request-Id"]

        # Should be a valid UUID
        try:
            uuid.UUID(request_id)
        except ValueError:
            pytest.fail(f"Generated request ID '{request_id}' is not a valid UUID")

    @pytest.mark.asyncio
    async def test_handles_call_next_exception(self):
        """Test that exceptions from call_next are properly propagated."""
        # Arrange
        request = Mock(spec=Request)
        request.headers = {}

        call_next = AsyncMock()
        call_next.side_effect = ValueError("Test exception")

        # Act & Assert
        with pytest.raises(ValueError, match="Test exception"):
            await add_request_id(request, call_next)
