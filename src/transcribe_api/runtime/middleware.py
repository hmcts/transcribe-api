import contextvars
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response

# Context variable for storing request ID
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


async def add_request_id(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Middleware to add a unique request ID to each request.

    This middleware extracts or generates a unique request ID for each incoming
    request, stores it in a context variable for use throughout the request
    lifecycle, and adds it to the response headers.

    Parameters
    ----------
    request : Request
        The incoming FastAPI request object containing headers and other
        request information.
    call_next : Callable[[Request], Awaitable[Response]]
        The next middleware or route handler in the FastAPI middleware chain.
        This function will be called to continue processing the request.

    Returns
    -------
    Response
        The response from the next middleware/handler with the X-Request-Id
        header added. If the request already had an X-Request-Id header, that
        value is used; otherwise, a new UUID is generated.

    Notes
    -----
    The request ID is stored in a context variable (request_id_ctx) which can
    be accessed from anywhere within the same request context, including
    exception handlers and other middleware.

    Examples
    --------
    >>> # This middleware is automatically applied by FastAPI
    >>> # The request ID will be available in response headers
    >>> response.headers["X-Request-Id"]
    '550e8400-e29b-41d4-a716-446655440000'
    """
    rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request_id_ctx.set(rid)
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    return response
