"""Payment HTTP contract and response validation without contacting Moyasar."""

import base64
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.core.config import settings
from app.integrations.moyasar_client import MoyasarClient, MoyasarInvalidResponseError
from app.workers import moyasar_worker


class MoyasarClientTests(unittest.TestCase):
    def setUp(self):
        self.payment_id = str(uuid4())
        patcher = patch.object(settings, 'MOYASAR_SECRET_KEY', 'sk_test_unit_test')
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, response):
        real_client = httpx.Client
        def handler(request):
            self.assertEqual(request.method, 'GET')
            self.assertEqual(str(request.url), f'https://api.moyasar.com/v1/payments/{self.payment_id}')
            expected = base64.b64encode(b'sk_test_unit_test:').decode()
            self.assertEqual(request.headers['authorization'], 'Basic '+expected)
            return response
        with patch('app.integrations.moyasar_client.httpx.Client',
                   side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)):
            return MoyasarClient().get_payment(self.payment_id)

    def test_payment_lookup_uses_basic_auth_and_parses_fields(self):
        result = self.fetch(httpx.Response(200, json={
            'id': self.payment_id, 'status': 'paid', 'amount': 10000, 'currency': 'AED',
        }))
        self.assertEqual(result.amount, 10000)
        self.assertEqual(result.id, self.payment_id)

    def test_bad_json_and_bad_field_types_have_safe_retryable_error(self):
        valid = {'id': self.payment_id, 'status': 'paid', 'amount': 10000, 'currency': 'AED'}
        responses = [httpx.Response(200, text='private response'),
                     httpx.Response(200, json={'private': 'secret'}),
                     httpx.Response(200, json={**valid, 'amount': True}),
                     httpx.Response(200, json={**valid, 'amount': '10000'}),
                     httpx.Response(200, json={**valid, 'status': 'x'*300})]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(MoyasarInvalidResponseError) as error:
                self.fetch(response)
            self.assertEqual(str(error.exception), 'Invalid Moyasar payment response')

    def test_http_error_remains_an_http_error(self):
        with self.assertRaises(httpx.HTTPStatusError):
            self.fetch(httpx.Response(503, json={'error': 'unavailable'}))

    def test_worker_rejects_placeholder_before_opening_database(self):
        for key in ('', 'sk_test_', 'sk_test_YOUR_KEY', 'sk_live_example'):
            with self.subTest(key=key), patch.object(settings, 'MOYASAR_SECRET_KEY', key), patch.object(
                moyasar_worker.SessionLocal, 'begin'
            ) as begin:
                with self.assertRaisesRegex(RuntimeError, 'Configure a real Moyasar test API key'):
                    moyasar_worker.main()
                begin.assert_not_called()
