import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.dependencies import get_current_user
from app.core.config import settings
from app.core.enums import Currency, WalletStatus
from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.models.wallet import Wallet
from app.services.recipient_service import RecipientService, mask_name


class RecipientServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        sender = User(
            username="Sender User", email="sender@example.com", mobile="11111111",
            date_of_birth=date(2000, 1, 1), password_hash="unused",
        )
        recipient = User(
            username="Ahmed Mohamed", email="recipient@example.com", mobile="22222222",
            date_of_birth=date(2000, 1, 1), password_hash="unused",
        )
        self.db.add_all([sender, recipient])
        self.db.flush()
        self.user_id = sender.id
        self.sender = Wallet(user_id=sender.id, currency=Currency.AED)
        self.recipient = Wallet(user_id=recipient.id, currency=Currency.AED)
        self.other_currency = Wallet(user_id=recipient.id, currency=Currency.USD)
        self.db.add_all([self.sender, self.recipient, self.other_currency])
        self.db.commit()
        self.service = RecipientService(self.db)

    def resolve(self, recipient_id=None, sender_id=None):
        return self.service.resolve(
            self.user_id,
            self.sender.id if sender_id is None else sender_id,
            self.recipient.id if recipient_id is None else recipient_id,
        )

    def test_active_recipient_returns_only_masked_name_id_and_currency(self):
        self.assertEqual(self.resolve().model_dump(), {
            "wallet_id": self.recipient.id,
            "display_name": "Ahmed M.",
            "currency": "AED",
        })

    def test_missing_inactive_and_wrong_currency_have_same_404(self):
        errors = []
        for recipient_id in (99999, self.other_currency.id):
            with self.assertRaises(HTTPException) as caught:
                self.resolve(recipient_id=recipient_id)
            errors.append((caught.exception.status_code, caught.exception.detail))
        for status in (WalletStatus.INACTIVE, WalletStatus.SUSPENDED):
            self.recipient.status = status
            self.db.commit()
            with self.assertRaises(HTTPException) as caught:
                self.resolve()
            errors.append((caught.exception.status_code, caught.exception.detail))
        self.assertEqual(errors, [(404, "Recipient wallet not found")] * 4)

    def test_sender_must_exist_and_belong_to_current_user(self):
        for sender_id in (99999, self.recipient.id):
            with self.assertRaises(HTTPException) as caught:
                self.resolve(sender_id=sender_id)
            self.assertEqual(caught.exception.status_code, 404)

    def test_same_wallet_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            self.resolve(recipient_id=self.sender.id)
        self.assertEqual(caught.exception.status_code, 400)

    def test_mask_name_matches_proposed_rules(self):
        for name, expected in (
            ("", "Nexa Wallet user"), ("   ", "Nexa Wallet user"),
            ("Ahmed", "A***"), (" Ahmed   Ali Mohamed ", "Ahmed M."),
            ("أحمد محمد", "أحمد م."),
        ):
            self.assertEqual(mask_name(name), expected)


class RecipientRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        RecipientServiceTests.setUp(self)
        self.payload = {
            "sender_wallet_id": self.sender.id,
            "recipient_wallet_id": self.recipient.id,
        }
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=self.user_id)
        self.redis = AsyncMock()
        self.redis.eval.return_value = 1
        self.redis_patcher = patch("app.api.routes.wallet.redis_client", self.redis)
        self.redis_patcher.start()
        self.addCleanup(self.redis_patcher.stop)
        self.limit_patcher = patch.object(settings, "RECIPIENT_LOOKUP_DAILY_LIMIT", 50)
        self.limit_patcher.start()
        self.addCleanup(self.limit_patcher.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides = self.old_overrides

    async def lookup(self, **changes):
        return await self.client.post(
            "/wallets/recipient/resolve", json={**self.payload, **changes}
        )

    async def test_success_returns_only_the_proposed_response_fields(self):
        response = await self.lookup()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "wallet_id": self.recipient.id, "display_name": "Ahmed M.", "currency": "AED",
        })
        args = self.redis.eval.await_args.args
        self.assertEqual(args[1:], (
            2, f"recipient_lookup_rate:{self.user_id}:minute",
            f"recipient_lookup_rate:{self.user_id}:day", 60, 86400, 5, 50,
        ))

    async def test_unknown_and_different_currency_both_return_404(self):
        responses = [await self.lookup(recipient_wallet_id=wallet_id)
                     for wallet_id in (99999, self.other_currency.id)]
        self.assertTrue(all(r.status_code == 404 for r in responses))
        self.assertEqual(responses[0].json(), responses[1].json())

    async def test_rejections_are_logged_without_recipient_details(self):
        with self.assertLogs("app.api.routes.wallet", level="WARNING") as logs:
            response = await self.lookup(recipient_wallet_id=self.other_currency.id)
        self.assertEqual(response.status_code, 404)
        output = " ".join(logs.output)
        self.assertIn(f"user_id={self.user_id}", output)
        self.assertNotIn("Ahmed", output)
        self.assertNotIn("recipient@example.com", output)

    async def test_rate_limit_rejects_before_recipient_database_lookup(self):
        self.redis.eval.return_value = 0
        with patch("app.api.routes.wallet.RecipientService") as service:
            response = await self.lookup()
        self.assertEqual(response.status_code, 429)
        service.assert_not_called()

    async def test_redis_failure_does_not_allow_unlimited_lookup(self):
        self.redis.eval.side_effect = RedisConnectionError("private-redis-detail")
        with patch("app.api.routes.wallet.RecipientService") as service:
            response = await self.lookup()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-redis-detail", response.text)
        service.assert_not_called()

    async def test_authentication_is_required_before_rate_limit(self):
        del app.dependency_overrides[get_current_user]
        response = await self.lookup()
        self.assertEqual(response.status_code, 401)
        self.redis.eval.assert_not_awaited()

    async def test_positive_wallet_ids_are_required(self):
        for changes in ({"sender_wallet_id": 0}, {"recipient_wallet_id": -1}):
            response = await self.lookup(**changes)
            self.assertEqual(response.status_code, 422)

    async def test_sender_ownership_and_same_wallet_are_enforced_over_http(self):
        response = await self.lookup(sender_wallet_id=self.recipient.id)
        self.assertEqual(response.status_code, 404)
        response = await self.lookup(recipient_wallet_id=self.sender.id)
        self.assertEqual(response.status_code, 400)
