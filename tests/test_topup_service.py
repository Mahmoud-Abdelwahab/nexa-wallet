"""Check payment ID lifetime using isolated data, without creating real payments."""

from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import Currency, WalletStatus
from app.database import Base
from app.models import TopUp, User, Wallet
from app.services.topup_service import TopUpService, TopUpIdempotencyConflictError
from app.models.topup import TopUpStatus


class TopUpServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = Session(self.engine)
        self.addCleanup(self.db.close)
        user = User(
            username="test", email="test@example.com", mobile="123456789",
            date_of_birth=date(2000, 1, 1), password_hash="unused",
        )
        self.db.add(user)
        self.db.flush()
        wallet = Wallet(user_id=user.id, currency=Currency.AED, balance=Decimal("50"))
        self.db.add(wallet)
        self.db.commit()
        self.user_id, self.wallet_id = user.id, wallet.id
        self.service = TopUpService(self.db)
        patcher = patch.object(settings, "MOYASAR_TOPUP_ALLOWED_CURRENCIES", "AED")
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, key="topup-1", amount="3", wallet_id=None):
        return self.service.create_topup(
            self.user_id, wallet_id or self.wallet_id, Decimal(amount), key,
        )

    def test_different_keys_create_distinct_requests_without_credit(self):
        first = self.create()
        second = self.create(key="topup-2")
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.moyasar_payment_id, second.moyasar_payment_id)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TopUp)), 2)
        self.assertEqual(self.db.get(Wallet, self.wallet_id).balance, Decimal("50"))

    def test_same_key_and_equivalent_amount_return_saved_ids(self):
        first = self.create()
        ids = first.id, first.moyasar_payment_id
        self.db.expunge_all()
        second = self.create(amount="3.00")
        self.assertEqual((second.id, second.moyasar_payment_id), ids)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TopUp)), 1)

    def test_changed_amount_or_wallet_conflicts_without_changes(self):
        first = self.create()
        ids = first.id, first.moyasar_payment_id
        for changes in ({"amount": "4"}, {"wallet_id": 999}):
            with self.subTest(changes=changes), self.assertRaises(TopUpIdempotencyConflictError):
                self.create(**changes)
        self.assertEqual((first.id, first.moyasar_payment_id), ids)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TopUp)), 1)
        self.assertEqual(self.db.get(Wallet, self.wallet_id).balance, Decimal("50"))

    def test_replay_returns_current_status_even_if_wallet_now_inactive(self):
        first = self.create()
        first.status = TopUpStatus.REVIEW_REQUIRED
        self.db.get(Wallet, self.wallet_id).status = WalletStatus.INACTIVE
        self.db.commit()
        second = self.create()
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.status, TopUpStatus.REVIEW_REQUIRED)

    def test_failed_commit_releases_key_for_retry(self):
        with patch.object(self.db, "commit", side_effect=RuntimeError("commit failed")):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TopUp)), 0)
        self.create()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TopUp)), 1)

    def test_invalid_input_does_not_create_request(self):
        for amount in ("0", "-1", "NaN", "sNaN", "Infinity", "1e999999", "3.001"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.create(amount=amount)
        for key in ("", "   ", "x" * 129):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.create(key=key)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TopUp)), 0)

    def test_same_key_is_scoped_to_user(self):
        first = self.create()
        user = User(username="other", email="other@example.com", mobile="987654321",
                    date_of_birth=date(2000, 1, 1), password_hash="unused")
        self.db.add(user)
        self.db.flush()
        wallet = Wallet(user_id=user.id, currency=Currency.AED, balance=Decimal("0"))
        self.db.add(wallet)
        self.db.commit()
        other = self.service.create_topup(user.id, wallet.id, Decimal("3"), "topup-1")
        self.assertNotEqual(first.id, other.id)
        self.assertNotEqual(first.moyasar_payment_id, other.moyasar_payment_id)

    def test_api_requires_key_and_returns_409_for_changed_data(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.api.routes.dependencies import get_current_user
        from app.database import get_db
        # Exercise real routing with isolated DB and an authenticated test user.
        previous = app.dependency_overrides.copy()
        app.dependency_overrides[get_current_user] = lambda: User(id=self.user_id)
        app.dependency_overrides[get_db] = lambda: self.db
        self.addCleanup(lambda: (app.dependency_overrides.clear(), app.dependency_overrides.update(previous)))
        # Use a mocked service here: real database semantics are tested above and in PostgreSQL.
        with TestClient(app) as client, patch.object(TopUpService, "create_topup") as create:
            body = {"wallet_id": self.wallet_id, "amount": "3"}
            self.assertEqual(client.post("/topups", json=body).status_code, 422)
            create.assert_not_called()
            create.side_effect = TopUpIdempotencyConflictError("Idempotency key used with different data")
            response = client.post("/topups", json=body, headers={"Idempotency-Key": "topup-1"})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(create.call_args.kwargs["idempotency_key"], "topup-1")

    def test_repeated_reads_keep_original_payment_id(self):
        topup = self.create()
        topup_id, payment_id = topup.id, topup.moyasar_payment_id
        for _ in range(3):
            self.db.expunge_all()
            result = self.service.get_topup(topup_id, self.user_id)
            self.assertEqual(result.moyasar_payment_id, payment_id)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TopUp)), 1)

    def test_another_user_cannot_read_payment_id(self):
        topup = self.create()
        with self.assertRaisesRegex(LookupError, "Top up not found"):
            self.service.get_topup(topup.id, self.user_id + 1)
