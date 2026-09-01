"""Unit tests for webhook dispatcher."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from transcribe_api.domain.webhook import _sign, verify_signature


class TestSign:
    def test_produces_consistent_signature(self):
        sig1 = _sign("hello", "secret")
        sig2 = _sign("hello", "secret")
        assert sig1 == sig2

    def test_different_bodies_produce_different_signatures(self):
        assert _sign("hello", "secret") != _sign("world", "secret")

    def test_different_secrets_produce_different_signatures(self):
        assert _sign("hello", "secret1") != _sign("hello", "secret2")


class TestVerifySignature:
    def test_accepts_valid_signature(self):
        body = '{"job_id": "123"}'
        secret = "my-secret"
        sig = f"sha256={_sign(body, secret)}"
        assert verify_signature(body, secret, sig) is True

    def test_rejects_tampered_body(self):
        secret = "my-secret"
        sig = f"sha256={_sign('original', secret)}"
        assert verify_signature("tampered", secret, sig) is False

    def test_rejects_wrong_secret(self):
        body = '{"job_id": "123"}'
        sig = f"sha256={_sign(body, 'correct-secret')}"
        assert verify_signature(body, "wrong-secret", sig) is False


class TestDispatch:
    @pytest.mark.asyncio
    async def test_returns_true_on_200(self):
        from transcribe_api.domain.webhook import dispatch

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await dispatch("https://cb.example.com", "secret", {"job_id": "123"})

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_after_all_retries_fail(self):
        import httpx

        from transcribe_api.domain.webhook import dispatch

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await dispatch(
                    "https://cb.example.com", "secret", {"job_id": "123"}, max_retries=2
                )

        assert result is False

    @pytest.mark.asyncio
    async def test_includes_signature_header(self):
        from transcribe_api.domain.webhook import _sign, dispatch

        mock_response = MagicMock()
        mock_response.status_code = 200
        captured_headers = {}

        async def capture_post(url, content, headers):
            captured_headers.update(headers)
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = capture_post

            payload = {"job_id": "123"}
            await dispatch("https://cb.example.com", "mysecret", payload)

        body = json.dumps(payload, default=str)
        expected_sig = f"sha256={_sign(body, 'mysecret')}"
        assert captured_headers["X-Signature-256"] == expected_sig

    @pytest.mark.asyncio
    async def test_retries_on_5xx(self):
        from transcribe_api.domain.webhook import dispatch

        ok_response = MagicMock()
        ok_response.status_code = 200

        fail_response = MagicMock()
        fail_response.status_code = 503

        responses = [fail_response, ok_response]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=responses)

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await dispatch("https://cb.example.com", "secret", {}, max_retries=3)

        assert result is True
