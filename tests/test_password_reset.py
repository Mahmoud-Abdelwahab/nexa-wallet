"""Password recovery uses isolated SQLite data and never sends real email."""

import hashlib
import smtplib
import threading
import unittest
from datetime import date
from unittest.mock import AsyncMock, Mock, patch

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.authentication import get_auth_service
from app.api.routes.password_reset import get_password_reset_service
from app.core.config import settings
from app.core.errors import BusinessError
from app.core.security import create_access_token, verify_password
from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.email_service import send_reset_email
from app.services.password_reset_service import FORGOT_PASSWORD_MESSAGE, PasswordResetService
from app.services.password_reset_token_store import (
    RESET_TTL_SECONDS, consume_reset_token, create_reset_token, reset_key,
)
from app.services.refresh_token_service import RefreshTokenService


class ResetTokenTests(unittest.IsolatedAsyncioTestCase):
    def test_refresh_rotation_preserves_version_and_legacy_defaults_to_zero(self):
        service = RefreshTokenService()
        _, _, session = service.create_refresh_session(42, 7)
        _, _, rotated = service.create_rotated_token(session)
        self.assertEqual(rotated.token_version, 7)
        self.assertEqual(rotated.session_id, session.session_id)
        self.assertEqual(rotated.absolute_expires_at, session.absolute_expires_at)
        from app.schemas.refresh_session import RefreshSession
        legacy = session.model_dump(exclude={"token_version"})
        self.assertEqual(RefreshSession.model_validate(legacy).token_version, 0)

    async def test_only_hash_is_stored_with_fifteen_minute_expiry(self):
        redis = AsyncMock()
        first = await create_reset_token(redis, 42, 7)
        second = await create_reset_token(redis, 42, 7)
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 43)
        redis.set.assert_any_await(
            "password_reset:" + hashlib.sha256(first.encode()).hexdigest(),
            "42:7", ex=900,
        )

    async def test_consumption_supports_bytes_strings_and_missing_tokens(self):
        redis = AsyncMock()
        for value, expected in ((b"42:7", (42, 7)), ("42:7", (42, 7)), (None, None)):
            redis.getdel.return_value = value
            self.assertEqual(await consume_reset_token(redis, "token"), expected)
        redis.getdel.assert_awaited_with(reset_key("token"))


class PasswordResetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(
            username="Reset Test", email="reset@example.com", mobile="123456789",
            date_of_birth=date(2000, 1, 1), password_hash="old-hash", token_version=0,
        )
        self.db.add(self.user)
        self.db.commit()
        self.user_id = self.user.id
        self.redis = AsyncMock()
        self.redis.eval.return_value = 1
        self.values = {}

        async def store(key, value, **kwargs):
            self.values[key] = value

        async def consume(key):
            return self.values.pop(key, None)

        self.redis.set.side_effect = store
        self.redis.getdel.side_effect = consume
        self.service = PasswordResetService(self.db, self.redis)
        self.service.refresh_token_store = Mock(revoke_all_sessions=AsyncMock())
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_password_reset_service] = lambda: self.service
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides = self.old_overrides
        self.db.close()
        self.engine.dispose()

    async def token(self):
        return await create_reset_token(self.redis, self.user_id, 0)

    async def test_known_unknown_and_limited_requests_have_identical_responses(self):
        responses = []
        with patch("app.services.password_reset_service.send_reset_email", new_callable=AsyncMock) as send:
            for email in (" RESET@EXAMPLE.COM ", "unknown@example.com"):
                response = await self.client.post("/auth/forgot-password", json={"email": email})
                responses.append((response.status_code, response.json()))
            self.redis.eval.return_value = 0
            response = await self.client.post("/auth/forgot-password", json={"email": self.user.email})
            responses.append((response.status_code, response.json()))
        self.assertEqual(responses, [(200, {"message": FORGOT_PASSWORD_MESSAGE})] * 3)
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[0], "reset@example.com")
        self.assertEqual(len(self.values), 1)
        rate_args = self.redis.eval.await_args.args
        self.assertNotIn(self.user.email, rate_args[2])
        self.assertEqual(rate_args[-1], RESET_TTL_SECONDS)

    async def test_reset_login_access_revocation_and_one_time_use(self):
        token, sibling = await self.token(), await self.token()
        old_access = create_access_token({"sub": str(self.user_id), "token_version": 0})
        response = await self.client.post("/auth/reset-password", json={
            "token": token, "new_password": "NewStrongPassword123!",
        })
        self.assertEqual(response.status_code, 200)
        self.db.expire_all()
        self.assertEqual(self.user.token_version, 1)
        self.assertTrue(verify_password("NewStrongPassword123!", self.user.password_hash))
        self.service.refresh_token_store.revoke_all_sessions.assert_awaited_once_with(self.user_id)
        auth = AuthService(self.db, RefreshTokenService(), Mock(store_session=AsyncMock()))
        app.dependency_overrides[get_auth_service] = lambda: auth
        login = await self.client.post("/auth/login", json={
            "email": self.user.email, "password": "NewStrongPassword123!",
        })
        self.assertEqual(login.status_code, 200)
        self.assertEqual(auth.refresh_token_store.store_session.await_args.kwargs["session"].token_version, 1)
        wrong = await self.client.post("/auth/login", json={
            "email": self.user.email, "password": "old-password",
        })
        self.assertEqual(wrong.status_code, 400)
        access = await self.client.get("/users/me", headers={"Authorization": f"Bearer {old_access}"})
        self.assertEqual(access.status_code, 401)
        # Both reuse and other tokens issued before the reset are invalid.
        for invalid in (token, sibling, "unknown-token"):
            response = await self.client.post("/auth/reset-password", json={
                "token": invalid, "new_password": "AnotherPassword123!",
            })
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"detail": "Invalid or expired reset token"})

    async def test_invalid_password_does_not_consume_token_or_echo_secrets(self):
        token = await self.token()
        response = await self.client.post("/auth/reset-password", json={
            "token": token, "new_password": "secret",
        })
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("secret", response.text)
        self.assertNotIn(token, response.text)
        self.redis.getdel.assert_not_awaited()
        self.assertIn(reset_key(token), self.values)
        with self.assertRaises(ValidationError):
            await self.service.reset_password(token, "short")
        self.redis.getdel.assert_not_awaited()

    async def test_missing_user_token_is_rejected(self):
        token = await create_reset_token(self.redis, self.user_id + 1, 0)
        with self.assertRaisesRegex(BusinessError, "Invalid or expired"):
            await self.service.reset_password(token, "NewPassword123!")
        self.service.refresh_token_store.revoke_all_sessions.assert_not_awaited()

    async def test_commit_failure_rolls_back_and_burns_token(self):
        token = await self.token()
        with patch.object(self.db, "commit", side_effect=OperationalError("update", {}, Exception("private"))):
            response = await self.client.post("/auth/reset-password", json={
                "token": token, "new_password": "NewPassword123!",
            })
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        self.db.expire_all()
        self.assertEqual((self.user.password_hash, self.user.token_version), ("old-hash", 0))
        self.assertNotIn(reset_key(token), self.values)
        self.service.refresh_token_store.revoke_all_sessions.assert_not_awaited()

    async def test_redis_consumption_failure_never_changes_password(self):
        self.redis.getdel.side_effect = RedisConnectionError("private")
        response = await self.client.post("/auth/reset-password", json={
            "token": "test-token", "new_password": "NewPassword123!",
        })
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.user.password_hash, "old-hash")

    async def test_cleanup_failure_still_invalidates_old_refresh_sessions(self):
        token = await self.token()
        refresh_service = RefreshTokenService()
        _, _, old_session = refresh_service.create_refresh_session(self.user_id, 0)
        store = self.service.refresh_token_store
        store.revoke_all_sessions.side_effect = RedisConnectionError("private")
        await self.service.reset_password(token, "NewPassword123!")
        self.db.expire_all()
        store.get_session = AsyncMock(return_value=old_session)
        store.rotate_session = AsyncMock()
        auth = AuthService(self.db, refresh_service, store)
        with self.assertRaisesRegex(BusinessError, "Invalid refresh token"):
            await auth.refresh_access_token("old-token")
        store.rotate_session.assert_not_awaited()

    async def test_smtp_failure_returns_generic_response_and_cleans_up_token(self):
        with patch("app.services.password_reset_service.send_reset_email", new_callable=AsyncMock,
                   side_effect=smtplib.SMTPException("private")):
            response = await self.client.post("/auth/forgot-password", json={"email": self.user.email})
        self.assertEqual((response.status_code, response.json()), (200, {"message": FORGOT_PASSWORD_MESSAGE}))
        self.redis.delete.assert_awaited_once_with(next(iter(self.values)))

    async def test_failed_rate_limit_storage_fails_closed(self):
        self.redis.eval.side_effect = RedisConnectionError("private")
        response = await self.client.post("/auth/forgot-password", json={"email": self.user.email})
        self.assertEqual(response.status_code, 503)
        self.redis.set.assert_not_awaited()

    async def test_database_and_hashing_run_in_worker(self):
        token = await self.token()
        loop_thread = threading.get_ident()

        def hash_in_worker(password):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return "new-hash"

        with patch("app.services.password_reset_service.hash_password", side_effect=hash_in_worker):
            await self.service.reset_password(token, "NewPassword123!")

    async def test_compare_and_swap_rejects_concurrent_version_change(self):
        token = await self.token()
        # Simulate another reset winning between SELECT and UPDATE.
        execute = self.db.execute

        def race(statement, *args, **kwargs):
            if getattr(statement, "is_update", False):
                return Mock(scalar_one_or_none=Mock(return_value=None))
            return execute(statement, *args, **kwargs)

        with patch.object(self.db, "execute", side_effect=race):
            with self.assertRaisesRegex(BusinessError, "Invalid or expired"):
                await self.service.reset_password(token, "NewPassword123!")
        self.db.expire_all()
        self.assertEqual(self.user.token_version, 0)


class EmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_mailpit_and_authenticated_starttls_share_sender(self):
        loop_thread = threading.get_ident()
        for tls, username in ((False, ""), (True, "sender@example.com")):
            with patch.object(settings, "SMTP_STARTTLS", tls), patch.object(settings, "SMTP_USERNAME", username), patch("app.services.email_service.smtplib.SMTP") as smtp:
                connection = smtp.return_value.__enter__.return_value

                def send(message):
                    self.assertNotEqual(threading.get_ident(), loop_thread)
                    self.assertEqual(message["To"], "reset@example.com")
                    self.assertIn("test-reset-token", message.get_content())

                connection.send_message.side_effect = send
                await send_reset_email("reset@example.com", "test-reset-token")
                self.assertEqual(connection.starttls.called, tls)
                self.assertEqual(connection.login.called, bool(username))
