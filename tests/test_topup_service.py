"""Check payment ID lifetime using isolated data, without creating real payments."""

from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import Currency
from app.database import Base
from app.models import TopUp, User, Wallet
from app.services.topup_service import TopUpService


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

    def create(self):
        return self.service.create_topup(self.user_id, self.wallet_id, Decimal("3"))

    def test_repeated_creation_creates_distinct_requests_without_credit(self):
        first = self.create()
        second = self.create()
        # Creation has no idempotency key yet: identical bodies create separate requests.
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.moyasar_payment_id, second.moyasar_payment_id)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TopUp)), 2)
        self.assertEqual(self.db.get(Wallet, self.wallet_id).balance, Decimal("50"))

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
