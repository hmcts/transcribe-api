from unittest.mock import patch

import pytest
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse

from transcribe_api.runtime.exception_handlers import http_exception_handler, unhandled_exception_handler
from transcribe_api.runtime.middleware import request_id_ctx


class TestHttpExceptionHandler:
    """Test cases for the http_exception_handler function."""

    @pytest.mark.asyncio
    async def test_handles_http_exception_with_request_id(self):
        """Test that HTTP exceptions are handled with request ID from context."""
        # Arrange
        exc = FastAPIHTTPException(status_code=404, detail="Not found")

        # Set context variable
        test_request_id = "test-request-id-123"
        request_id_ctx.set(test_request_id)

        # Act
        response = await http_exception_handler(exc)

        # Assert
        assert isinstance(response, JSONResponse), "Response should be a JSONResponse instance"
        assert response.status_code == 404, f"Status code should be 404, got {response.status_code}"
        assert response.headers["X-Request-Id"] == test_request_id, (
            f"Request ID should be '{test_request_id}', got '{response.headers['X-Request-Id']}'"
        )

        # Check response content
        content = response.body.decode()
        assert "Not found" in content, "Response content should contain 'Not found'"
        assert test_request_id in content, f"Response content should contain request ID '{test_request_id}'"

    @pytest.mark.asyncio
    async def test_handles_http_exception_without_request_id(self):
        """Test that HTTP exceptions are handled when no request ID in context."""
        # Arrange
        exc = FastAPIHTTPException(status_code=400, detail="Bad request")

        # Clear context variable
        request_id_ctx.set("")

        # Act
        response = await http_exception_handler(exc)

        # Assert
        assert isinstance(response, JSONResponse), "Response should be a JSONResponse instance"
        assert response.status_code == 400, f"Status code should be 400, got {response.status_code}"
        assert response.headers["X-Request-Id"] == "", (
            f"Request ID should be empty string, got '{response.headers['X-Request-Id']}'"
        )

        # Check response content
        content = response.body.decode()
        assert "Bad request" in content, "Response content should contain 'Bad request'"

    @pytest.mark.asyncio
    async def test_preserves_exception_status_code_and_detail(self):
        """Test that the original exception status code and detail are preserved."""
        # Arrange
        test_detail = "Custom error message with special characters: !@#$%^&*()"
        exc = FastAPIHTTPException(status_code=422, detail=test_detail)

        request_id_ctx.set("test-request-id")

        # Act
        response = await http_exception_handler(exc)

        # Assert
        assert response.status_code == 422, f"Status code should be 422, got {response.status_code}"
        content = response.body.decode()
        assert test_detail in content, f"Response content should contain detail '{test_detail}'"


class TestUnhandledExceptionHandler:
    """Test cases for the unhandled_exception_handler function."""

    @pytest.mark.asyncio
    @patch("transcribe_api.runtime.exception_handlers.sentry_sdk.capture_exception")
    async def test_handles_unhandled_exception_with_sentry(self, mock_capture_exception):
        """Test that unhandled exceptions are captured by Sentry."""
        # Arrange
        exc = ValueError("Test unhandled exception")

        test_request_id = "test-request-id-456"
        request_id_ctx.set(test_request_id)

        # Act
        response = await unhandled_exception_handler(exc)

        # Assert
        assert isinstance(response, JSONResponse), "Response should be a JSONResponse instance"
        assert response.status_code == 500, f"Status code should be 500, got {response.status_code}"
        assert response.headers["X-Request-Id"] == test_request_id, (
            f"Request ID should be '{test_request_id}', got '{response.headers['X-Request-Id']}'"
        )

        # Check response content
        content = response.body.decode()
        assert "Internal Server Error" in content, "Response content should contain 'Internal Server Error'"
        assert test_request_id in content, f"Response content should contain request ID '{test_request_id}'"

        # Verify Sentry was called
        mock_capture_exception.assert_called_once_with(exc)

    @pytest.mark.asyncio
    @patch("transcribe_api.runtime.exception_handlers.sentry_sdk.capture_exception")
    async def test_handles_unhandled_exception_without_request_id(self, mock_capture_exception):
        """Test that unhandled exceptions are handled when no request ID in context."""
        # Arrange
        exc = RuntimeError("Test runtime error")

        # Clear context variable
        request_id_ctx.set("")

        # Act
        response = await unhandled_exception_handler(exc)

        # Assert
        assert isinstance(response, JSONResponse), "Response should be a JSONResponse instance"
        assert response.status_code == 500, f"Status code should be 500, got {response.status_code}"
        assert response.headers["X-Request-Id"] == "", (
            f"Request ID should be empty string, got '{response.headers['X-Request-Id']}'"
        )

        # Verify Sentry was called
        mock_capture_exception.assert_called_once_with(exc)

    @pytest.mark.asyncio
    @patch("transcribe_api.runtime.exception_handlers.sentry_sdk.capture_exception")
    async def test_sentry_capture_exception_failure(self, mock_capture_exception):
        """Test that handler continues to work even if Sentry capture fails."""
        # Arrange
        exc = ValueError("Test exception")

        # Make Sentry capture fail
        mock_capture_exception.side_effect = Exception("Sentry failure")

        request_id_ctx.set("test-request-id")

        # Act
        response = await unhandled_exception_handler(exc)

        # Assert
        assert isinstance(response, JSONResponse), "Response should be a JSONResponse instance"
        assert response.status_code == 500, f"Status code should be 500, got {response.status_code}"
        assert response.headers["X-Request-Id"] == "test-request-id", (
            f"Request ID should be 'test-request-id', got '{response.headers['X-Request-Id']}'"
        )

        # Verify Sentry was called despite failure
        mock_capture_exception.assert_called_once_with(exc)
