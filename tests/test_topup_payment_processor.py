"""Financial posting and review rules with isolated SQLite data and fake payments.

SQLite verifies balances/rollback here; real PostgreSQL locks need integration tests.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
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
from app.repositories.payment_job_repository import PaymentJobRepository
from app.workers.moyasar_worker import process_one


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
            return process_one(MoyasarClient(), self.sessions)

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
            self.assertEqual(db.get(Event, self.event_id).attempts, 1)

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

    def test_failed_payment_is_terminal_with_durable_support_alert(self):
        self.process(self.payment(status='failed'))
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.FAILED, 0, 0))
        with self.sessions() as db:
            event = db.get(Event, self.event_id)
            self.assertIsNotNone(event.support_alert_at)
            self.assertIsNotNone(event.processed_at)
            self.assertEqual(event.last_error, 'Moyasar payment failed; no wallet credit posted')
        self.assertFalse(self.process())

    def test_http_failure_schedules_retry_without_credit(self):
        self.assertTrue(self.process(error=httpx.ConnectError('test failure')))
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
        with self.sessions() as db:
            event = db.get(Event, self.event_id)
            self.assertEqual(event.attempts, 1)
            self.assertIsNone(event.processed_at)
            self.assertEqual(event.last_error, 'Moyasar network error')
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
            self.assertFalse(process_one(MoyasarClient(), self.sessions))
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
        # The committed lease survives posting rollback; recovery waits for expiry.
        with self.sessions.begin() as db:
            db.get(Event, self.event_id).next_attempt_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        self.process()
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))

    def test_completed_review_is_not_automatically_credited(self):
        with self.sessions.begin() as db:
            db.get(TopUp, self.topup_id).status = TopUpStatus.REVIEW_REQUIRED
        self.process()
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.REVIEW_REQUIRED, 0, 0))

    def claim(self):
        with self.sessions.begin() as db:
            return PaymentJobRepository(db).claim_next()

    def expire_lease(self):
        with self.sessions.begin() as db:
            db.get(Event, self.event_id).next_attempt_at = datetime.now(timezone.utc)-timedelta(seconds=1)

    def test_claim_commits_lease_and_hides_job_until_expiry(self):
        job = self.claim()
        self.assertEqual(job.attempt, 1)
        self.assertIsNone(self.claim())
        self.expire_lease()
        next_job = self.claim()
        self.assertEqual(next_job.attempt, 2)
        self.assertEqual(job.event_id, next_job.event_id)

    def test_stale_worker_cannot_credit_fail_or_reschedule_new_claim(self):
        old = self.claim()
        self.expire_lease()
        current = self.claim()
        with self.sessions.begin() as db:
            processor = TopUpPaymentProcessor(db)
            processor.finish_payment(old, self.payment())
            processor.finish_payment(old, self.payment(status='failed'))
            processor.finish_retry(old, 'stale error')
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
        with self.sessions() as db:
            event = db.get(Event, self.event_id)
            self.assertIsNone(event.last_error)
            self.assertIsNone(event.support_alert_at)
        with self.sessions.begin() as db:
            TopUpPaymentProcessor(db).finish_payment(current, self.payment())
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))

    def test_503_slows_down_and_alerts_without_stopping_recovery(self):
        request = httpx.Request('GET', 'https://api.moyasar.com/v1/payments/test')
        error = httpx.HTTPStatusError('not found', request=request,
                                     response=httpx.Response(503, request=request))
        for attempt in range(1, 9):
            before = datetime.now(timezone.utc)
            self.assertTrue(self.process(error=error))
            with self.sessions() as db:
                event = db.get(Event, self.event_id)
                self.assertEqual(event.attempts, attempt)
                self.assertEqual(db.scalar(select(func.count()).select_from(Event)), 1)
                if attempt < 8:
                    self.assertIsNone(event.processed_at)
                    self.assertIsNone(event.support_alert_at)
                    deadline = event.next_attempt_at.replace(tzinfo=timezone.utc)
                    self.assertGreaterEqual(deadline, before + timedelta(minutes=min(2**attempt, 60)))
                else:
                    self.assertIsNone(event.processed_at)
                    self.assertIsNotNone(event.support_alert_at)
                    self.assertIn('503', event.last_error)
            if attempt < 8:
                self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
                self.expire_lease()
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
        self.assertFalse(self.process())

        self.expire_lease()
        self.process()
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))

    def test_late_paid_response_after_eight_claims_can_still_credit(self):
        with self.sessions.begin() as db:
            db.get(Event, self.event_id).attempts = 8
        self.assertTrue(self.process())
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))

    def test_http_call_has_no_checked_out_database_connection(self):
        checked_out = set()
        def checkout(connection, record, proxy):
            checked_out.add(id(record))
        def checkin(connection, record):
            checked_out.discard(id(record))
        sqlalchemy_event.listen(self.engine, 'checkout', checkout)
        sqlalchemy_event.listen(self.engine, 'checkin', checkin)
        try:
            def fetch(payment_id):
                self.assertEqual(checked_out, set())
                # The claim is already visible from a fresh session.
                with self.sessions() as db:
                    self.assertEqual(db.get(Event, self.event_id).attempts, 1)
                return self.payment()
            with patch.object(MoyasarClient, 'get_payment', side_effect=fetch):
                process_one(MoyasarClient(), self.sessions)
        finally:
            sqlalchemy_event.remove(self.engine, 'checkout', checkout)
            sqlalchemy_event.remove(self.engine, 'checkin', checkin)
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))

    def test_failed_topup_is_not_reopened_by_later_paid_event(self):
        self.process(self.payment(status='failed'))
        with self.sessions.begin() as db:
            db.add(Event(id='later-paid', payment_id=self.payment_id, event_type='payment_paid',
                         next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
        self.process()
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.FAILED, 0, 0))
        with self.sessions() as db:
            self.assertIsNotNone(db.get(Event, self.event_id).support_alert_at)
            self.assertIsNone(db.get(Event, 'later-paid').support_alert_at)

    def test_alert_rolls_back_with_failed_finalization_and_is_not_logged(self):
        def fail_alert(session, context, instances):
            if any(isinstance(item, Event) and item.support_alert_at is not None
                   for item in session.dirty):
                raise RuntimeError('alert write failed')
        sqlalchemy_event.listen(Session, 'before_flush', fail_alert)
        try:
            with patch('app.workers.moyasar_worker.logger.error') as log:
                with self.assertRaisesRegex(RuntimeError, 'alert write failed'):
                    self.process(self.payment(status='failed'))
                log.assert_not_called()
        finally:
            sqlalchemy_event.remove(Session, 'before_flush', fail_alert)
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
        with self.sessions() as db:
            self.assertIsNone(db.get(Event, self.event_id).support_alert_at)

    def test_in_progress_and_auth_errors_do_not_mean_payment_failed(self):
        for status in ('initiated', 'authorized'):
            self.process(self.payment(status=status))
            self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
            self.expire_lease()
        for status in (401, 403, 429, 503):
            request = httpx.Request('GET', 'https://api.moyasar.com/v1/payments/test')
            error = httpx.HTTPStatusError('error', request=request,
                                         response=httpx.Response(status, request=request))
            self.process(error=error)
            self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
            self.expire_lease()

    def test_404_keeps_pending_after_many_checks_then_recovers_without_webhook(self):
        with self.sessions.begin() as db:
            db.get(TopUp, self.topup_id).created_at = datetime.now(timezone.utc)-timedelta(hours=2)
            db.get(Event, self.event_id).attempts = 8
        request = httpx.Request('GET', 'https://api.moyasar.com/v1/payments/test')
        error = httpx.HTTPStatusError('missing', request=request,
                                     response=httpx.Response(404, request=request))
        before = datetime.now(timezone.utc)
        self.process(error=error)
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
        with self.sessions() as db:
            event = db.get(Event, self.event_id)
            self.assertEqual(event.attempts, 9)
            self.assertIsNone(event.processed_at)
            self.assertIsNone(event.support_alert_at)
            self.assertGreaterEqual(event.next_attempt_at.replace(tzinfo=timezone.utc),
                                    before + timedelta(minutes=60))
        # The same saved event discovers the late paid payment, even without a webhook.
        self.expire_lease()
        self.process()
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))

    def test_early_404_checks_after_two_minutes(self):
        request = httpx.Request('GET', 'https://api.moyasar.com/v1/payments/test')
        error = httpx.HTTPStatusError('missing', request=request,
                                     response=httpx.Response(404, request=request))
        before = datetime.now(timezone.utc)
        self.process(error=error)
        with self.sessions() as db:
            deadline = db.get(Event, self.event_id).next_attempt_at.replace(tzinfo=timezone.utc)
            self.assertGreaterEqual(deadline, before + timedelta(minutes=2))
            self.assertLess(deadline, before + timedelta(minutes=3))

    def test_late_webhook_credits_before_next_scheduled_check_without_duplicates(self):
        with self.sessions.begin() as db:
            db.get(TopUp, self.topup_id).created_at = datetime.now(timezone.utc)-timedelta(hours=1)
            db.get(Event, self.event_id).attempts = 10
        self.process(self.payment(status='initiated'))
        self.assertEqual(self.snapshot(), (Decimal('50'), TopUpStatus.PENDING, 0, 0))
        with self.sessions.begin() as db:
            db.add(Event(id='late-webhook', payment_id=self.payment_id, event_type='payment_paid',
                         next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
        self.process()
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))
        self.expire_lease()
        self.process()
        self.assertEqual(self.snapshot(), (Decimal('150'), TopUpStatus.COMPLETED, 1, 1))


# Beginner pytest examples: fake records and a mocked Session, no real DB or HTTP.

def make_data(topup_status=TopUpStatus.PENDING):
    # Arrange: prepare fake database records
    event = SimpleNamespace(
        id="E100",
        payment_id="P123",
        attempts=1,
        processed_at=None,
        support_alert_at=None,
        last_error=None,
    )

    topup = SimpleNamespace(
        id="T100",
        moyasar_payment_id="P123",
        wallet_id=1,
        user_id=1,
        amount_minor=5000,
        currency=Currency.AED,
        status=topup_status,
        transaction_id=None,
    )

    wallet = SimpleNamespace(
        id=1,
        user_id=1,
        balance=Decimal("100.00"),
        currency=Currency.AED,
        status=WalletStatus.ACTIVE,
    )

    job = SimpleNamespace(
        event_id="E100",
        payment_id="P123",
        attempt=1,
    )

    payment = SimpleNamespace(
        id="P123",
        status="paid",
        amount=5000,
        currency="AED",
    )

    db = MagicMock()

    return db, event, topup, wallet, job, payment


def test_successful_topup():
    db, event, topup, wallet, job, payment = make_data()
    # Each scalar() call receives the next fake result: Event -> TopUp -> Wallet.
    # This tests the processor's decisions, not real SQL queries or database locks.
    db.scalar.side_effect = [event, topup, wallet]

    # Simulate database-generated transaction ID after flush
    def fake_flush():
        transaction = db.add.call_args_list[0].args[0]
        transaction.id = 500

    db.flush.side_effect = fake_flush

    # Act
    processor = TopUpPaymentProcessor(db)
    processor.finish_payment(job, payment)

    # Assert
    assert wallet.balance == Decimal("150.00")
    assert topup.status == TopUpStatus.COMPLETED
    assert topup.transaction_id == 500
    assert event.processed_at is not None
    assert db.add.call_count == 2
    transaction = db.add.call_args_list[0].args[0]
    ledger = db.add.call_args_list[1].args[0]
    assert isinstance(transaction, Transaction)
    assert transaction.amount == Decimal("50.00")
    assert transaction.status.value == "SUCCESS"
    assert isinstance(ledger, LedgerEntry)
    assert ledger.transaction_id == 500
    assert ledger.wallet_id == wallet.id
    assert ledger.amount == Decimal("50.00")
    assert ledger.direction.value == "CREDIT"
    assert ledger.entry_type.value == "TOP_UP"
    db.commit.assert_not_called()  # The worker owns the final commit.


def test_duplicate_payment():
    # A previous attempt already completed this TopUp; no new credit is allowed.
    db, event, topup, wallet, job, payment = make_data(
        TopUpStatus.COMPLETED
    )

    db.scalar.side_effect = [event, topup]

    processor = TopUpPaymentProcessor(db)
    processor.finish_payment(job, payment)

    assert wallet.balance == Decimal("100.00")
    assert topup.status == TopUpStatus.COMPLETED
    assert event.processed_at is not None
    db.add.assert_not_called()


def test_amount_mismatch():
    db, event, topup, wallet, job, payment = make_data()

    # Nexa expects 5000 fils, Moyasar returns 1000
    payment.amount = 1000

    db.scalar.side_effect = [event, topup]

    processor = TopUpPaymentProcessor(db)
    processor.finish_payment(job, payment)

    assert topup.status == TopUpStatus.REVIEW_REQUIRED
    assert wallet.balance == Decimal("100.00")
    assert event.support_alert_at is not None
    assert event.processed_at is not None
    assert event.last_error == "Amount or currency mismatch"
    db.add.assert_not_called()
