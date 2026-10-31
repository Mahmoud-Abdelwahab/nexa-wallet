"""Check that sync database/password work does not run on the async loop."""

import inspect
import threading
import unittest
from datetime import date
from unittest.mock import AsyncMock, Mock, patch

from app.api.routes import authentication, health, transaction, transfer, users, wallet
from app.mappers.user_mapper import UserMapper
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.refresh_token_service import RefreshTokenService


class ExecutionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.loop_thread = threading.get_ident()
        self.user = User(
            id=99, username="Test User", email="test@example.com",
            mobile="971000000000", password_hash="test-hash", token_version=0,
        )
        self.db = Mock()
        self.store = Mock()
        for name in ("store_session", "get_session", "rotate_session",
                     "revoke_session", "revoke_all_sessions"):
            setattr(self.store, name, AsyncMock(side_effect=self.on_loop))
        self.service = AuthService(self.db, RefreshTokenService(), self.store)
        self.service.user_repository = Mock()
        self.service.wallet_repository = Mock()
        self.db.commit.side_effect = self.in_worker
        self.db.flush.side_effect = self.in_worker
        self.db.rollback.side_effect = self.in_worker

    def in_worker(self, *args, **kwargs):
        self.assertNotEqual(threading.get_ident(), self.loop_thread)

    async def on_loop(self, *args, **kwargs):
        self.assertEqual(threading.get_ident(), self.loop_thread)

    def load_user(self, *args):
        self.in_worker()
        return self.user

    def serialize_user(self, user):
        self.in_worker()
        return self.original_mapper(user)

    async def test_login_database_password_and_serialization_use_worker(self):
        self.service.user_repository.get_by_email.side_effect = self.load_user
        self.original_mapper = UserMapper.to_response
        with patch("app.services.auth_service.verify_password", side_effect=lambda *a: (self.in_worker(), True)[1]), patch.object(UserMapper, "to_response", side_effect=self.serialize_user):
            response = await self.service.login_user("TEST@example.com", "password")
        self.assertEqual(response.user.id, self.user.id)
        self.service.user_repository.get_by_email.assert_called_once_with("test@example.com")
        self.store.store_session.assert_awaited_once()

    async def test_missing_login_still_verifies_dummy_hash_in_worker(self):
        def missing(*args):
            self.in_worker()
            return None
        self.service.user_repository.get_by_email.side_effect = missing
        with patch("app.services.auth_service.verify_password", side_effect=self.in_worker) as verify:
            with self.assertRaisesRegex(ValueError, "Invalid email or password"):
                await self.service.login_user("missing@example.com", "wrong")
        verify.assert_called_once()
        self.store.store_session.assert_not_awaited()

    def configure_registration(self):
        def absent(*args):
            self.in_worker()
            return None
        def create(user):
            self.in_worker()
            user.id = 99
            user.token_version = 0
        self.service.user_repository.get_by_mobile.side_effect = absent
        self.service.user_repository.get_by_email.side_effect = absent
        self.service.user_repository.create.side_effect = create
        self.service.wallet_repository.create.side_effect = self.in_worker

    async def test_registration_database_and_hashing_use_worker(self):
        self.configure_registration()
        def hash_password(password):
            self.in_worker()
            return "test-hash"
        with patch("app.services.auth_service.hash_password", side_effect=hash_password):
            response = await self.service.register_user("Test User", "test@example.com", "password", "971000000000", date(2000, 1, 1))
        self.assertEqual(response.user.id, 99)
        self.db.commit.assert_called_once()
        self.store.store_session.assert_awaited_once()

    async def test_registration_failure_rolls_back_in_worker(self):
        self.configure_registration()
        def fail_flush():
            self.in_worker()
            raise RuntimeError("Simulated insert failure")
        self.db.flush.side_effect = fail_flush
        with patch("app.services.auth_service.hash_password", return_value="test-hash"):
            with self.assertRaisesRegex(RuntimeError, "Simulated insert failure"):
                await self.service.register_user("Test User", "test@example.com", "password", "971000000000", date(2000, 1, 1))
        self.db.rollback.assert_called_once()
        self.db.commit.assert_not_called()
        self.store.store_session.assert_not_awaited()

    async def test_refresh_keeps_redis_on_loop_and_orm_reads_in_worker(self):
        _, _, session = RefreshTokenService().create_refresh_session(self.user.id)
        async def get_session(*args):
            await self.on_loop()
            return session
        async def rotate(**kwargs):
            await self.on_loop()
            return True
        self.store.get_session.side_effect = get_session
        self.store.rotate_session.side_effect = rotate
        self.service.user_repository.get_by_id.side_effect = self.load_user
        self.original_mapper = UserMapper.to_response
        with patch.object(UserMapper, "to_response", side_effect=self.serialize_user):
            response = await self.service.refresh_access_token("old-token")
        self.assertEqual(response.user.id, self.user.id)
        self.assertNotEqual(response.refresh_token, "old-token")
        self.store.rotate_session.assert_awaited_once()

    async def test_logout_all_offloads_version_update_and_commit(self):
        await self.service.logout_all(self.user)
        self.assertEqual(self.user.token_version, 1)
        self.db.commit.assert_called_once()
        self.store.revoke_all_sessions.assert_awaited_once_with(self.user.id)

    async def test_route_execution_modes(self):
        sync_routes = (
            wallet.create_wallet, wallet.get_wallets, wallet.get_wallet,
            wallet.get_wallet_statement, transfer.create_transfer,
            transaction.get_transactions, transaction.get_transaction,
            users.get_me, users.update_me, users.change_password,
        )
        for endpoint in sync_routes:
            self.assertFalse(inspect.iscoroutinefunction(endpoint), endpoint.__name__)
        for endpoint in (authentication.register, authentication.login,
                         authentication.refresh, authentication.logout,
                         authentication.logout_all, health.redis_health):
            self.assertTrue(inspect.iscoroutinefunction(endpoint), endpoint.__name__)
