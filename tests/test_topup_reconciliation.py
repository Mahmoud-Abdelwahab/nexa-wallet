"""Producer -> saved queue -> consumer; isolated data and simulated Moyasar replies."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import Mock
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.core.enums import Currency
from app.models import User, Wallet, TopUp, Transaction, LedgerEntry
from app.models.moyasar_webhook_event import MoyasarWebhookEventRecord as Event
from app.integrations.moyasar_client import MoyasarPayment
from app.services.topup_reconciliation_service import TopUpReconciliationService
from app.workers.moyasar_worker import process_one


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        self.payment_id = str(uuid4())
        with self.sessions.begin() as db:
            user = User(username='test', email='test@example.com', mobile='123',
                        date_of_birth=date(2000, 1, 1), password_hash='unused')
            db.add(user); db.flush()
            wallet = Wallet(user_id=user.id, currency=Currency.AED, balance=Decimal('50'))
            db.add(wallet); db.flush()
            topup = TopUp(user_id=user.id, wallet_id=wallet.id, amount_minor=5000,
                          currency=Currency.AED, moyasar_payment_id=self.payment_id,
                          created_at=datetime.now(timezone.utc)-timedelta(minutes=6))
            db.add(topup); db.flush()
            self.topup_id, self.wallet_id = topup.id, wallet.id
        self.event_id = f'reconcile:{self.topup_id}'
        self.client = Mock()

    def enqueue(self):
        with self.sessions.begin() as db:
            return TopUpReconciliationService(db).enqueue_pending_topups()

    def make_due(self):
        with self.sessions.begin() as db:
            db.get(Event, self.event_id).next_attempt_at = datetime.now(timezone.utc)-timedelta(seconds=1)

    def http_error(self, status):
        request = httpx.Request('GET', 'https://api.moyasar.com/v1/payments/test')
        return httpx.HTTPStatusError('simulated', request=request,
                                     response=httpx.Response(status, request=request))

    def assert_pending(self):
        with self.sessions() as db:
            self.assertEqual(db.get(TopUp, self.topup_id).status.value, 'PENDING')
            self.assertEqual(db.get(Wallet, self.wallet_id).balance, Decimal('50'))
            self.assertEqual(db.scalar(select(func.count()).select_from(Transaction)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(LedgerEntry)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Event)), 1)
            self.assertIsNone(db.get(Event, self.event_id).processed_at)

    def test_404_stays_pending_slows_down_and_does_not_enqueue_duplicates(self):
        self.assertEqual(self.enqueue(), 1)
        self.client.get_payment.side_effect = self.http_error(404)
        for attempt in range(1, 10):
            before = datetime.now(timezone.utc)
            self.assertTrue(process_one(self.client, self.sessions))
            self.assert_pending()
            self.assertEqual(self.enqueue(), 0)
            with self.sessions() as db:
                event = db.get(Event, self.event_id)
                self.assertEqual(event.attempts, attempt)
                self.assertIsNone(event.support_alert_at)
                deadline = event.next_attempt_at.replace(tzinfo=timezone.utc)
                minutes = 2 if attempt < 8 else 60
                self.assertGreaterEqual(deadline, before + timedelta(minutes=minutes))
                self.assertLess(deadline, before + timedelta(minutes=minutes+1))
            self.make_due()

    def test_timeout_and_5xx_use_backoff_then_keep_pending_with_one_alert(self):
        self.assertEqual(self.enqueue(), 1)
        for attempt in range(1, 10):
            error = httpx.ReadTimeout('simulated') if attempt % 2 else self.http_error(503)
            self.client.get_payment.side_effect = error
            before = datetime.now(timezone.utc)
            process_one(self.client, self.sessions)
            self.assert_pending()
            with self.sessions() as db:
                event = db.get(Event, self.event_id)
                deadline = event.next_attempt_at.replace(tzinfo=timezone.utc)
                self.assertGreaterEqual(deadline, before + timedelta(minutes=min(2**attempt, 60)))
                if attempt == 8:
                    alert_at = event.support_alert_at
                    self.assertIsNotNone(alert_at)
                if attempt == 9:
                    self.assertEqual(event.support_alert_at, alert_at)
            self.make_due()

    def test_paid_verifies_and_credits_once_even_when_webhook_arrives_later(self):
        self.assertEqual(self.enqueue(), 1)
        self.client.get_payment.return_value = MoyasarPayment(
            id=self.payment_id, status='paid', amount=5000, currency='AED')
        self.assertTrue(process_one(self.client, self.sessions))
        self.assertEqual(self.enqueue(), 0)
        self.assertFalse(process_one(self.client, self.sessions))
        with self.sessions.begin() as db:
            db.add(Event(id='late-webhook', payment_id=self.payment_id, event_type='payment_paid',
                         next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
        self.assertTrue(process_one(self.client, self.sessions))
        with self.sessions() as db:
            self.assertEqual(db.get(TopUp, self.topup_id).status.value, 'COMPLETED')
            self.assertEqual(db.get(Wallet, self.wallet_id).balance, Decimal('100'))
            self.assertEqual(db.scalar(select(func.count()).select_from(Transaction)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(LedgerEntry)), 1)
            self.assertIsNotNone(db.get(Event, self.event_id).processed_at)
