"""Offline contract tests: no real FX requests, Redis or PostgreSQL needed."""
import asyncio
import json
import unittest
from datetime import date, timedelta
from decimal import Decimal

import httpx

from app.core.enums import Currency, WalletStatus
from app.integrations.frankfurter_client import (
    ExchangeRateUnavailable,
    FrankfurterClient,
    ReferenceRate,
)
from app.main import app
from app.models.wallet import Wallet
from app.services.wallet_summary_service import calculate_wallet_summary


class MemoryCache:
    def __init__(self):
        self.items = {}
        self.ttls = []

    async def get(self, key):
        return self.items.get(key)

    async def set(self, key, value, ex):
        self.items[key] = value
        self.ttls.append(ex)


def wallet(wallet_id, currency, balance, status=WalletStatus.ACTIVE):
    return Wallet(
        id=wallet_id,
        user_id=1,
        currency=currency,
        status=status,
        balance=Decimal(balance),
    )


class WalletSummaryMathTests(unittest.TestCase):
    def test_same_currency_needs_no_fx_and_no_estimate_label(self):
        result = calculate_wallet_summary(
            [wallet(1, Currency.AED, "100.1234")],
            Currency.AED,
            {},
        )
        self.assertEqual(result.estimated_total, Decimal("100.12"))
        self.assertFalse(result.is_estimate)
        self.assertIsNone(result.rates_as_of)
        self.assertEqual(result.wallets[0].rate_to_base, Decimal("1"))

    def test_mixed_currency_inverts_base_to_quote_rate(self):
        # Rates are USD/EUR PER AED, not AED per USD/EUR.
        today = date.today()
        result = calculate_wallet_summary(
            [
                wallet(1, Currency.AED, "100"),
                wallet(2, Currency.USD, "100"),
                wallet(3, Currency.EUR, "50", WalletStatus.SUSPENDED),
            ],
            Currency.AED,
            {
                Currency.USD: ReferenceRate(Decimal("0.25"), today),
                Currency.EUR: ReferenceRate(Decimal("0.50"), today),
            },
        )
        self.assertEqual(
            [line.estimated_in_base for line in result.wallets],
            [Decimal("100.00"), Decimal("400.00"), Decimal("100.00")],
        )
        self.assertEqual(result.estimated_total, Decimal("600.00"))
        self.assertEqual(result.wallets[1].rate_to_base, Decimal("4.000000000000"))
        self.assertEqual(result.wallets[2].status, WalletStatus.SUSPENDED)
        self.assertTrue(result.is_estimate)
        self.assertEqual(result.rates_as_of, today)

    def test_never_return_partial_total_if_rate_is_missing(self):
        with self.assertRaises(ExchangeRateUnavailable):
            calculate_wallet_summary(
                [wallet(1, Currency.USD, "100")], Currency.AED, {},
            )

    def test_displayed_rows_add_up_to_displayed_total(self):
        today = date.today()
        result = calculate_wallet_summary(
            [
                wallet(1, Currency.USD, "0.01"),
                wallet(2, Currency.EUR, "0.01"),
            ],
            Currency.AED,
            {
                Currency.USD: ReferenceRate(Decimal("0.29"), today),
                Currency.EUR: ReferenceRate(Decimal("0.26"), today),
            },
        )
        self.assertEqual(
            result.estimated_total,
            sum((x.estimated_in_base for x in result.wallets), Decimal("0")),
        )

    def test_route_precedes_wallet_id_path(self):
        paths = [r.path for r in app.router.routes]
        self.assertLess(paths.index("/wallets/summary"), paths.index("/wallets/{wallet_id}"))


class FrankfurterClientTests(unittest.IsolatedAsyncioTestCase):
    def make_http(self, rows):
        self.calls = 0

        def handler(request):
            self.calls += 1
            self.assertEqual(request.url.path, "/v2/rates")
            self.assertEqual(request.url.params["base"], "AED")
            self.assertEqual(
                set(request.url.params["quotes"].split(",")), {"EUR", "USD"},
            )
            return httpx.Response(200, json=rows)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_fetch_validates_shape_and_caches(self):
        today = date.today().isoformat()
        rows = [
            {"date": today, "base": "AED", "quote": "USD", "rate": 0.25},
            {"date": today, "base": "AED", "quote": "EUR", "rate": 0.5},
        ]
        cache = MemoryCache()
        async with self.make_http(rows) as http:
            client = FrankfurterClient(cache, http_client=http)
            first = await client.get_rates(Currency.AED, {Currency.USD, Currency.EUR})
            second = await client.get_rates(Currency.AED, {Currency.EUR, Currency.USD})
        self.assertEqual(first, second)
        self.assertEqual(first[Currency.USD].quote_per_base, Decimal("0.25"))
        self.assertEqual(self.calls, 1)
        self.assertEqual(cache.ttls, [21600])

    async def test_missing_quote_and_old_rates_fail_closed(self):
        today = date.today().isoformat()
        rows = [
            {"date": today, "base": "AED", "quote": "USD", "rate": 0.25},
        ]
        async with self.make_http(rows) as http:
            with self.assertRaises(ExchangeRateUnavailable):
                await FrankfurterClient(MemoryCache(), http).get_rates(
                    Currency.AED, {Currency.USD, Currency.EUR},
                )
        old = (date.today() - timedelta(days=20)).isoformat()
        rows = [
            {"date": old, "base": "AED", "quote": "USD", "rate": 0.25},
            {"date": old, "base": "AED", "quote": "EUR", "rate": 0.5},
        ]
        async with self.make_http(rows) as http:
            with self.assertRaises(ExchangeRateUnavailable):
                await FrankfurterClient(MemoryCache(), http).get_rates(
                    Currency.AED, {Currency.USD, Currency.EUR},
                )

    async def test_no_external_request_for_same_currency(self):
        result = await FrankfurterClient(MemoryCache()).get_rates(
            Currency.AED, {Currency.AED},
        )
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
