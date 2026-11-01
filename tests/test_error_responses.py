"""Exercise HTTP error responses without a live database or user balances."""

import asyncio
import json
import unittest
from datetime import date
from app.services.auth_service import AuthService
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.api.routes.authentication import get_auth_service
from app.api.routes.dependencies import get_current_user
from app.core.errors import BusinessError
from app.database import get_db
from app.main import app


async def request(method, path, body=None, headers=()):
    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": json.dumps(body).encode()}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "server": ("test", 80),
        "client": ("test", 123),
        "headers": [(b"content-type", b"application/json"), *headers],
    }
    try:
        await app(scope, receive, send)
    except Exception:
        # Starlette re-raises internal failures after sending the safe 500 response.
        if not sent or sent[0].get("status") != 500:
            raise
    return sent[0]["status"], json.loads(b"".join(
        message.get("body", b"") for message in sent
    ))


class ErrorResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = lambda: Mock()

    def tearDown(self):
        app.dependency_overrides = self.old_overrides

    async def test_bad_token_subject_and_missing_user_have_same_response(self):
        responses = []
        for subject in ("invalid", [], None, "123"):
            with patch('app.api.routes.dependencies.decode_access_token',
                       return_value={"sub": subject, "token_version": 0}), patch(
                       'app.api.routes.dependencies.UserRepository') as repo:
                repo.return_value.get_by_id.return_value = None
                responses.append(await request("GET", "/transactions", headers=[
                    (b"authorization", b"Bearer test-token")]))
        self.assertTrue(all(r == (401, {"detail": "Invalid authentication credentials"})
                            for r in responses))

    async def test_validation_does_not_echo_password(self):
        status, body = await request("POST", "/auth/register", {
            "username": "tester", "email": "test@example.com", "password": "secret",
            "mobile": "12345678", "date_of_birth": "2000-01-01",
        })
        self.assertEqual(status, 422)
        self.assertNotIn("secret", json.dumps(body))
        self.assertNotIn('"input"', json.dumps(body))
        self.assertIn("password", json.dumps(body))

    async def test_internal_errors_are_not_returned_but_business_errors_are(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        for error, expected_status, expected_detail in (
            (ValueError("SQL password=private-value"), 500,
             "An unexpected error occurred. Please try again later."),
            (RuntimeError("Idempotency record missing"), 500,
             "An unexpected error occurred. Please try again later."),
            (BusinessError("Insufficient balance"), 400, "Insufficient balance"),
        ):
            with self.subTest(error=type(error).__name__), patch(
                'app.api.routes.transfer.TransferService'
            ) as service:
                service.return_value.transfer.side_effect = error
                status, body = await request("POST", "/transfers", {
                    "sender_wallet_id": 1, "receiver_wallet_id": 2, "amount": "150",
                }, [(b"idempotency-key", b"test-key")])
                self.assertEqual((status, body),
                                 (expected_status, {"detail": expected_detail}))

    async def test_auth_unexpected_value_error_is_not_exposed(self):
        service = SimpleNamespace(login_user=AsyncMock(side_effect=ValueError("private")))
        app.dependency_overrides[get_auth_service] = lambda: service
        status, body = await request("POST", "/auth/login", {
            "email": "test@example.com", "password": "test-password",
        })
        self.assertEqual(status, 500)
        self.assertNotIn("private", json.dumps(body))

    async def test_duplicate_contacts_have_one_registration_message(self):
        service = AuthService(Mock(), Mock(), Mock())
        for duplicate_mobile in (True, False):
            service.user_repository.get_by_mobile = Mock(return_value=object() if duplicate_mobile else None)
            service.user_repository.get_by_email = Mock(return_value=object())
            with self.assertRaisesRegex(BusinessError, "^Unable to register with these details$"):
                service._register_user_sync("tester", "test@example.com", "password",
                                            "12345678", date(2000, 1, 1))
