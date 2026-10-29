import logging

import pytest
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse

from transcribe_api.runtime.exception_handlers import http_exception_handler, unhandled_exception_handler


class TestHttpExceptionHandler:
    """Test cases for the http_exception_handler function."""

    @pytest.mark.asyncio
    async def test_handles_http_exception_with_request_id(self):
        """Test that HTTP exceptions are handled with the request ID passed in."""
        # Arrange
        exc = FastAPIHTTPException(status_code=404, detail="Not found")

        test_request_id = "test-request-id-123"
        request_id = test_request_id

        # Act
        response = await http_exception_handler(exc, request_id)

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
        """Test that HTTP exceptions are handled when there is no request ID."""
        # Arrange
        exc = FastAPIHTTPException(status_code=400, detail="Bad request")

        request_id = ""

        # Act
        response = await http_exception_handler(exc, request_id)

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

        request_id = "test-request-id"

        # Act
        response = await http_exception_handler(exc, request_id)

        # Assert
        assert response.status_code == 422, f"Status code should be 422, got {response.status_code}"
        content = response.body.decode()
        assert test_detail in content, f"Response content should contain detail '{test_detail}'"


class TestUnhandledExceptionHandler:
    """The handler for unhandled exceptions: a generic 500, and the exception logged
    (type and a stack hmcts-otel redacts) for App Insights. No Sentry on CNP."""

    @pytest.mark.asyncio
    async def test_logs_the_exception_and_returns_a_generic_500(self):
        exc = ValueError("Test unhandled exception")
        records: list[logging.LogRecord] = []

        class Collect(logging.Handler):  # on the module's logger, before any export filter
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler_logger = logging.getLogger("transcribe_api.runtime.exception_handlers")
        collect = Collect()
        handler_logger.addHandler(collect)
        try:
            response = await unhandled_exception_handler(exc, "test-request-id-456")
        finally:
            handler_logger.removeHandler(collect)

        assert isinstance(response, JSONResponse)
        assert response.status_code == 500
        assert response.headers["X-Request-Id"] == "test-request-id-456"
        content = response.body.decode()
        assert "Internal Server Error" in content and "test-request-id-456" in content
        assert "Test unhandled exception" not in content  # never the exception's text
        (record,) = records
        assert record.levelno == logging.ERROR
        # The exception is attached: as exc_info, or (once hmcts-otel is configured) as
        # its type and a stack with the message redacted, which is what is exported.
        if record.exc_info is not None:
            assert record.exc_info[1] is exc
        else:
            assert record.__dict__["exception.type"] == "ValueError"
            assert "Test unhandled exception" not in record.__dict__["exception.stacktrace"]
        assert record.getMessage() == "Unhandled exception (ValueError)"

    @pytest.mark.asyncio
    async def test_handles_unhandled_exception_without_request_id(self):
        response = await unhandled_exception_handler(RuntimeError("Test runtime error"), "")

        assert response.status_code == 500
        assert response.headers["X-Request-Id"] == ""
