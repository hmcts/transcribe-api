"""The upload and job rate limits are per user, not per token (review finding S1).

The limiter used to key on the raw bearer token. A BenchNotes token is replaced every few minutes
and on demand (``PUT /api/me/context``), so every new token was a new, empty bucket.
"""

from __future__ import annotations

import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from transcribe_api.api.routes_recording import _caller_key
from transcribe_api.domain.auth import app_token
from transcribe_api.domain.auth.app_token import Context

JUDGE = Context("ET", "Judge")


def _token(user_id, **overrides) -> str:
    args = {"sysadmin": False, "assignments": [JUDGE], "ctx": JUDGE} | overrides
    return app_token.mint(user_id, **args)[0]


def _request(authorization: str | None) -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return Request({"type": "http", "headers": headers, "client": ("203.0.113.9", 1234)})


def test_two_tokens_of_one_user_share_a_key_and_two_users_do_not():
    alice, bob = uuid4(), uuid4()
    first = _caller_key(_request(f"Bearer {_token(alice, now=time.time())}"))
    second = _caller_key(_request(f"Bearer {_token(alice, now=time.time() - 10)}"))
    other = _caller_key(_request(f"Bearer {_token(bob)}"))

    assert first == second == f"user:{alice}"
    assert other != first


def test_a_token_that_is_not_ours_is_keyed_on_the_address_not_on_the_token():
    # An invalid bearer must not be a way to get a fresh bucket per made-up string.
    assert _caller_key(_request("Bearer not-a-token-1")) == "203.0.113.9"
    assert _caller_key(_request("Bearer not-a-token-2")) == "203.0.113.9"
    assert _caller_key(_request(None)) == "203.0.113.9"


def test_re_minting_a_token_does_not_reset_the_limit():
    limiter = Limiter(key_func=_caller_key)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.post("/limited")
    @limiter.limit("2/hour")
    def limited(request: Request) -> dict:
        return {"ok": True}

    client = TestClient(app)
    user = uuid4()
    statuses = []
    for n in range(4):
        token = _token(user, now=time.time() - n)  # a different token every time, as after a re-mint
        statuses.append(client.post("/limited", headers={"Authorization": f"Bearer {token}"}).status_code)
    assert statuses == [200, 200, 429, 429]
    # Someone else is not affected.
    assert client.post("/limited", headers={"Authorization": f"Bearer {_token(uuid4())}"}).status_code == 200
