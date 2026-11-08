"""Exercise durable receipt and duplicate handling without external payments."""

import inspect
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api.routes.moyasar_webhook import receive_moyasar_webhook, settings
from app.main import app
from app.models.moyasar_webhook_event import MoyasarWebhookEventRecord
from app.schemas.moyasar_webhook import MoyasarWebhookEvent


class MoyasarWebhookTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        MoyasarWebhookEventRecord.__table__.create(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        patcher = patch.object(settings, 'MOYASAR_WEBHOOK_SECRET', SecretStr('test-secret'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.payment_id = str(uuid4())

    def event(self, **changes):
        return MoyasarWebhookEvent(**{
            'id': 'event-1', 'type': 'payment_paid', 'live': False,
            'secret_token': 'test-secret',
            'data': {'id': self.payment_id, 'status': 'paid',
                     'source': {'card': 'must-not-be-stored'}},
            **changes,
        })

    def count(self):
        return self.db.scalar(select(func.count()).select_from(MoyasarWebhookEventRecord))

    def test_receipt_commits_only_processing_metadata(self):
        result = receive_moyasar_webhook(self.event(), self.db)
        self.assertEqual(result, {'received': True})
        # A new session can see the committed event.
        with Session(self.engine) as reader:
            record = reader.get(MoyasarWebhookEventRecord, 'event-1')
            self.assertEqual(record.payment_id, self.payment_id)
            self.assertEqual(record.event_type, 'payment_paid')
            self.assertEqual(record.attempts, 0)
            self.assertIsNone(record.processed_at)
            self.assertNotIn('secret_token', record.__table__.columns)
            self.assertNotIn('data', record.__table__.columns)

    def test_duplicate_event_does_not_reset_worker_progress(self):
        receive_moyasar_webhook(self.event(), self.db)
        record = self.db.get(MoyasarWebhookEventRecord, 'event-1')
        record.attempts = 2
        self.db.commit()
        result = receive_moyasar_webhook(self.event(), self.db)
        self.assertEqual(result, {'received': True})
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.db.get(MoyasarWebhookEventRecord, 'event-1').attempts, 2)

    def test_unsupported_event_is_ignored_without_write(self):
        result = receive_moyasar_webhook(self.event(type='payment_refunded', data={}), self.db)
        self.assertEqual(result, {'received': True, 'ignored': True})
        self.assertEqual(self.count(), 0)

    def test_failed_notification_is_queued_for_provider_verification(self):
        result = receive_moyasar_webhook(self.event(type='payment_failed'), self.db)
        self.assertEqual(result, {'received': True})
        record = self.db.get(MoyasarWebhookEventRecord, 'event-1')
        self.assertEqual(record.event_type, 'payment_failed')
        self.assertIsNone(record.processed_at)
        self.assertIsNone(record.support_alert_at)

    def test_verification_logs_only_code_without_financial_write(self):
        for code in ('012345', 123456):
            with self.subTest(code=code), self.assertLogs(
                'app.api.routes.moyasar_webhook', level='INFO'
            ) as logs:
                result = receive_moyasar_webhook(self.event(
                    type='webhook_verification',
                    data={'verification_code': code, 'extra': 'private-data'},
                ), self.db)
            self.assertEqual(result, {'received': True})
            self.assertEqual(logs.records[0].getMessage(),
                             f'Moyasar webhook verification code: {code}')
            self.assertEqual(self.count(), 0)

    def test_verification_rejects_invalid_codes_and_unauthenticated_requests(self):
        with patch('app.api.routes.moyasar_webhook.logger.info') as log:
            for code in (None, True, '12345', '123456\n', 'abcdef', '١٢٣٤٥٦', {}):
                with self.subTest(code=code), self.assertRaises(HTTPException) as error:
                    receive_moyasar_webhook(self.event(
                        type='webhook_verification', data={'verification_code': code},
                    ), self.db)
                self.assertEqual(error.exception.status_code, 422)
            with self.assertRaises(HTTPException) as error:
                receive_moyasar_webhook(self.event(
                    type='webhook_verification', secret_token='wrong',
                    data={'verification_code': '123456'},
                ), self.db)
            self.assertEqual(error.exception.status_code, 403)
            log.assert_not_called()
        self.assertEqual(self.count(), 0)

    def test_invalid_and_unicode_secrets_are_rejected(self):
        for secret in ('wrong', '', 'غير صحيح'):
            with self.subTest(secret=secret), self.assertRaises(HTTPException) as error:
                receive_moyasar_webhook(self.event(secret_token=secret), self.db)
            self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.count(), 0)

    def test_missing_configuration_fails_closed(self):
        for secret in (None, SecretStr('')):
            with patch.object(settings, 'MOYASAR_WEBHOOK_SECRET', secret):
                with self.assertRaises(HTTPException) as error:
                    receive_moyasar_webhook(self.event(secret_token=''), self.db)
                self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(self.count(), 0)

    def test_live_event_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            receive_moyasar_webhook(self.event(live=True), self.db)
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.count(), 0)

    def test_invalid_payment_ids_are_422(self):
        for data in ({}, {'id': None}, {'id': 1}, {'id': []}, {'id': {}}, {'id': 'bad'}):
            with self.subTest(data=data), self.assertRaises(HTTPException) as error:
                receive_moyasar_webhook(self.event(data=data), self.db)
            self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(self.count(), 0)

    def test_database_failure_rolls_back_without_acknowledgement(self):
        for method in ('execute', 'commit'):
            with self.subTest(method=method), patch.object(
                self.db, method, side_effect=RuntimeError('DB unavailable')
            ), patch.object(self.db, 'rollback', wraps=self.db.rollback) as rollback:
                with self.assertRaisesRegex(RuntimeError, 'DB unavailable'):
                    receive_moyasar_webhook(self.event(), self.db)
                rollback.assert_called_once()
            self.assertEqual(self.count(), 0)
        self.assertEqual(receive_moyasar_webhook(self.event(), self.db), {'received': True})

    def test_route_is_registered_and_synchronous(self):
        self.assertIn('post', app.openapi()['paths']['/webhooks/moyasar'])
        self.assertFalse(inspect.iscoroutinefunction(receive_moyasar_webhook))
