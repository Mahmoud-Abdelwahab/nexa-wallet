"""Sandbox webhook authentication and safe logging; no network or payments."""

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import SecretStr

from app.api.routes.moyasar_webhook import receive_moyasar_webhook, settings
from app.main import app
from app.schemas.moyasar_webhook import MoyasarWebhookEvent


class MoyasarWebhookTests(unittest.IsolatedAsyncioTestCase):
    def event(self, **changes):
        return MoyasarWebhookEvent(**{
            'id': 'event-1', 'type': 'payment_paid', 'live': False,
            'secret_token': 'test-secret',
            'data': {'id': 'payment-1', 'status': 'paid',
                     'source': {'card': 'card-data-must-not-be-logged'}},
            **changes,
        })

    async def test_valid_sandbox_event_logs_only_metadata(self):
        with patch.object(settings, 'MOYASAR_WEBHOOK_SECRET', SecretStr('test-secret')):
            with self.assertLogs('app.api.routes.moyasar_webhook', level='INFO') as logs:
                result = await receive_moyasar_webhook(self.event())
        self.assertEqual(result, {'received': True})
        output = ''.join(logs.output)
        for value in ('event-1', 'payment-1', 'paid'):
            self.assertIn(value, output)
        for value in ('test-secret', 'card-data-must-not-be-logged', 'secret_token'):
            self.assertNotIn(value, output)

    async def test_invalid_and_unicode_secrets_are_rejected(self):
        with patch.object(settings, 'MOYASAR_WEBHOOK_SECRET', SecretStr('test-secret')):
            for secret in ('wrong', '', 'غير صحيح'):
                with self.subTest(secret=secret), self.assertRaises(HTTPException) as error:
                    await receive_moyasar_webhook(self.event(secret_token=secret))
                self.assertEqual(error.exception.status_code, 403)

    async def test_missing_configuration_fails_closed(self):
        for secret in (None, SecretStr('')):
            with patch.object(settings, 'MOYASAR_WEBHOOK_SECRET', secret):
                with self.assertRaises(HTTPException) as error:
                    await receive_moyasar_webhook(self.event(secret_token=''))
                self.assertEqual(error.exception.status_code, 503)

    async def test_live_event_is_rejected(self):
        with patch.object(settings, 'MOYASAR_WEBHOOK_SECRET', SecretStr('test-secret')):
            with self.assertRaises(HTTPException) as error:
                await receive_moyasar_webhook(self.event(live=True))
            self.assertEqual(error.exception.status_code, 403)

    async def test_log_values_cannot_add_lines_or_dump_nested_data(self):
        with patch.object(settings, 'MOYASAR_WEBHOOK_SECRET', SecretStr('test-secret')):
            with self.assertLogs('app.api.routes.moyasar_webhook', level='INFO') as logs:
                await receive_moyasar_webhook(self.event(
                    id='event\nforged-line', data={'id': {'secret': 'private'}, 'status': ['private']},
                ))
        output = ''.join(logs.output)
        self.assertNotIn('\n', output)
        self.assertNotIn('private', output)

    def test_route_is_registered(self):
        self.assertIn('post', app.openapi()['paths']['/webhooks/moyasar'])
