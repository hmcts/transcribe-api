import contextlib

import sentry_sdk
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse

from .middleware import request_id_ctx


async def http_exception_handler(exc: FastAPIHTTPException) -> JSONResponse:
    """Handle expected HTTP exceptions (4xx, 5xx status codes).

    This handler processes HTTP exceptions that are explicitly raised by the
    application code (e.g., 404 Not Found, 400 Bad Request). It preserves the
    original status code and error message while adding request ID context
    for debugging and tracing purposes.

    Parameters
    ----------
    exc : FastAPIHTTPException
        The HTTP exception that was raised, containing the status code and
        detail message to be returned to the client.

    Returns
    -------
    JSONResponse
        A JSON response containing the error details and request ID. The
        response includes:
        - The original status code from the exception
        - The original error detail message
        - The request ID in both the response body and X-Request-Id header

    Notes
    -----
    This handler is designed for expected errors that should be shown to
    the client with specific error messages to help with debugging.

    Examples
    --------
    >>> # When a 404 exception is raised
    >>> response = await http_exception_handler(HTTPException(404, "Not found"))
    >>> response.status_code
    404
    >>> response.headers["X-Request-Id"]
    '<request_id>'
    """
    rid = request_id_ctx.get()
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": rid},
        # MERGE NOTE: recording routes raise HTTPException carrying headers
        # (e.g. Content-Range on a 416). The original handler built a fresh
        # response and dropped them, which silently broke range responses
        # once both route sets shared this handler.
        headers={**(exc.headers or {}), "X-Request-Id": rid},
    )


async def unhandled_exception_handler(exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions (unhandled errors, bugs, etc.).

    This handler processes unhandled exceptions that occur during request
    processing. It captures the exception in Sentry for monitoring and
    debugging, then returns a generic error message to the client to avoid
    exposing internal implementation details.

    Parameters
    ----------
    exc : Exception
        The unhandled exception that was raised. Can be any type of
        exception that wasn't caught by application code.

    Returns
    -------
    JSONResponse
        A JSON response with a generic 500 Internal Server Error message.
        The response includes:
        - Status code 500
        - Generic error message "Internal Server Error"
        - Request ID for debugging purposes
        - X-Request-Id header for tracing

    Notes
    -----
    This handler is designed for unexpected errors that should not expose
    internal details to the client. The full exception details are captured
    in Sentry for developer debugging.

    Examples
    --------
    >>> # When an unexpected exception occurs
    >>> response = await unhandled_exception_handler(ValueError("Unexpected error"))
    >>> response.status_code
    500
    >>> response.headers["X-Request-Id"]
    'request_id'
    """
    rid = request_id_ctx.get()
    with contextlib.suppress(Exception):
        # If Sentry fails, continue with error handling: tests disallow sockets
        sentry_sdk.capture_exception(exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "request_id": rid},
        headers={"X-Request-Id": rid},
    )
