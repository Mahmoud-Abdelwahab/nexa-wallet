"""Financial posting and review rules with isolated SQLite data and fake payments.

SQLite verifies balances/rollback here; real PostgreSQL locks need integration tests.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, event as sqlalchemy_event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import Currency, WalletStatus
from app.database import Base
from app.integrations.moyasar_client import (
    MoyasarClient, MoyasarInvalidResponseError, MoyasarPayment,
)
from app.models import LedgerEntry, TopUp, Transaction, User, Wallet
from app.models.moyasar_webhook_event import MoyasarWebhookEventRecord as Event
from app.models.topup import TopUpStatus
from app.schemas.transaction import TransactionResponse
from app.services.topup_payment_processor import TopUpPaymentProcessor


class TopUpProcessorTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        self.payment_id = str(uuid4())
        self.event_id = 'event-1'
        with self.sessions.begin() as db:
            user = User(username='test', email='test@example.com', mobile='123456789',
                        date_of_birth=date(2000, 1, 1), password_hash='unused')
            db.add(user)
            db.flush()
            wallet = Wallet(user_id=user.id, currency=Currency.AED, balance=Decimal('50'))
            db.add(wallet)
            db.flush()
            topup = TopUp(user_id=user.id, wallet_id=wallet.id, amount_minor=10000,
                          currency=Currency.AED, moyasar_payment_id=self.payment_id)
            db.add(topup)
            db.flush()
            self.user_id, self.wallet_id, self.topup_id = user.id, wallet.id, topup.id
            before = datetime.now(timezone.utc) - timedelta(minutes=5)
            db.add(Event(id=self.event_id, payment_id=self.payment_id,
                         event_type='payment_paid', created_at=before, next_attempt_at=before))

    def payment(self, **changes):
        return MoyasarPayment(**{'id': self.payment_id, 'status': 'paid',
                                 'amount': 10000, 'currency': 'AED', **changes})

    def process(self, payment=None, error=None):
        with patch.object(MoyasarClient, 'get_payment', return_value=payment or self.payment(),
                          side_effect=error):
            with self.sessions.begin() as db:
                return TopUpPaymentProcessor(db).process_next()

    def snapshot(self):
        with self.sessions() as db:
            return (db.get(Wallet, self.wallet_id).balance,
                    db.get(TopUp, self.topup_id).status,
                    db.scalar(select(func.count()).select_from(Transaction)),
                    db.scalar(select(func.count()).select_from(LedgerEntry)))

    def assert_review_without_credit(self, reason):
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.REVIEW_REQUIRED, 0, 0))
        with self.sessions() as db:
            event = db.get(Event, self.event_id)
            self.assertIsNotNone(event.processed_at)
            self.assertEqual(event.last_error, reason)

    def test_success_and_different_event_for_same_payment_credit_once(self):
        self.assertTrue(self.process())
        self.assertFalse(self.process())
        with self.sessions.begin() as db:
            db.add(Event(id='event-2', payment_id=self.payment_id, event_type='payment_paid',
                         next_attempt_at=datetime.now(timezone.utc)-timedelta(minutes=1)))
        self.assertTrue(self.process())
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))
        with self.sessions() as db:
            topup = db.get(TopUp, self.topup_id)
            self.assertEqual(topup.moyasar_payment_id, self.payment_id)
            tx = db.get(Transaction, topup.transaction_id)
            ledger = db.scalar(select(LedgerEntry))
            self.assertIsNone(TransactionResponse.model_validate(tx).sender_wallet_id)
            self.assertEqual(tx.receiver_wallet_id, self.wallet_id)
            self.assertEqual(tx.amount, Decimal('100'))
            self.assertEqual(tx.status.value, 'SUCCESS')
            self.assertEqual((ledger.transaction_id, ledger.wallet_id, ledger.direction.value,
                              ledger.entry_type.value, ledger.amount),
                             (tx.id, self.wallet_id, 'CREDIT', 'TOP_UP', Decimal('100')))

    def test_suspended_wallet_is_not_credited(self):
        with self.sessions.begin() as db:
            db.get(Wallet, self.wallet_id).status = WalletStatus.SUSPENDED
        self.process()
        self.assert_review_without_credit('Wallet is not active')

    def test_inactive_wallet_is_not_credited(self):
        with self.sessions.begin() as db:
            db.get(Wallet, self.wallet_id).status = WalletStatus.INACTIVE
        self.process()
        self.assert_review_without_credit('Wallet is not active')

    def test_id_mismatch_reviews_expected_topup_not_returned_topup(self):
        other_id = str(uuid4())
        with self.sessions.begin() as db:
            other = TopUp(user_id=self.user_id, wallet_id=self.wallet_id, amount_minor=10000,
                          currency=Currency.AED, moyasar_payment_id=other_id)
            db.add(other)
            db.flush()
            other_topup_id = other.id
        self.process(self.payment(id=other_id))
        self.assert_review_without_credit('Payment ID mismatch')
        with self.sessions() as db:
            self.assertEqual(db.get(TopUp, other_topup_id).status, TopUpStatus.PENDING)
            self.assertEqual(db.get(Event, self.event_id).attempts, 0)

    def test_mismatch_does_not_reopen_completed_topup(self):
        self.process()
        with self.sessions.begin() as db:
            db.add(Event(id='event-2', payment_id=self.payment_id, event_type='payment_paid',
                         next_attempt_at=datetime.now(timezone.utc)-timedelta(minutes=1)))
        self.process(self.payment(id=str(uuid4())))
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))

    def test_amount_mismatch_requires_review(self):
        self.process(self.payment(amount=9000))
        self.assert_review_without_credit('Amount or currency mismatch')

    def test_currency_mismatch_requires_review(self):
        self.process(self.payment(currency='USD'))
        self.assert_review_without_credit('Amount or currency mismatch')

    def test_failed_payment_requires_review(self):
        self.process(self.payment(status='failed'))
        self.assert_review_without_credit('Payment status: failed')

    def test_http_failure_schedules_retry_without_credit(self):
        self.assertTrue(self.process(error=httpx.ConnectError('test failure')))
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
        with self.sessions() as db:
            event = db.get(Event, self.event_id)
            self.assertEqual(event.attempts, 1)
            self.assertIsNone(event.processed_at)
            self.assertEqual(event.last_error, 'Moyasar API unavailable')
        self.assertFalse(self.process())  # Not due yet.

    def test_invalid_response_does_not_block_next_valid_event(self):
        self.process(error=MoyasarInvalidResponseError('invalid'))
        with self.sessions.begin() as db:
            other = TopUp(user_id=self.user_id, wallet_id=self.wallet_id, amount_minor=10000,
                          currency=Currency.AED, moyasar_payment_id=str(uuid4()))
            db.add(other)
            db.flush()
            other_payment_id = other.moyasar_payment_id
            db.add(Event(id='event-2', payment_id=other_payment_id, event_type='payment_paid',
                         next_attempt_at=datetime.now(timezone.utc)-timedelta(minutes=1)))
        self.assertTrue(self.process(self.payment(id=other_payment_id)))
        with self.sessions() as db:
            first = db.get(Event, self.event_id)
            self.assertEqual(first.attempts, 1)
            self.assertEqual(first.last_error, 'Invalid Moyasar payment response')
            self.assertIsNone(first.processed_at)
            self.assertIsNotNone(db.get(Event, 'event-2').processed_at)
            self.assertEqual(db.get(TopUp, self.topup_id).status, TopUpStatus.PENDING)
            self.assertEqual(db.get(Wallet, self.wallet_id).balance, Decimal('150'))

    def test_future_event_is_not_processed_early(self):
        with self.sessions.begin() as db:
            db.get(Event, self.event_id).next_attempt_at = datetime.now(timezone.utc)+timedelta(minutes=10)
        with patch.object(MoyasarClient, 'get_payment') as lookup:
            with self.sessions.begin() as db:
                self.assertFalse(TopUpPaymentProcessor(db).process_next())
            lookup.assert_not_called()

    def test_ledger_failure_rolls_back_all_posting_and_allows_retry(self):
        def fail_ledger(session, context, instances):
            if any(isinstance(item, LedgerEntry) for item in session.new):
                raise RuntimeError('ledger write failed')
        sqlalchemy_event.listen(Session, 'before_flush', fail_ledger)
        try:
            with self.assertRaisesRegex(RuntimeError, 'ledger write failed'):
                self.process()
        finally:
            sqlalchemy_event.remove(Session, 'before_flush', fail_ledger)
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
        with self.sessions() as db:
            self.assertIsNone(db.get(Event, self.event_id).processed_at)
            self.assertIsNone(db.get(TopUp, self.topup_id).transaction_id)
        self.process()
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))

    def test_completed_review_is_not_automatically_credited(self):
        with self.sessions.begin() as db:
            db.get(TopUp, self.topup_id).status = TopUpStatus.REVIEW_REQUIRED
        self.process()
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.REVIEW_REQUIRED, 0, 0))
