"""Offline contract tests: no real FX requests, Redis or PostgreSQL needed."""
import json
import threading
import unittest
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.dependencies import get_current_user
from app.api.routes.wallet import router as wallet_router
from app.core.enums import Currency, WalletStatus
from app.integrations.frankfurter_client import (
    ExchangeRateUnavailable,
    FrankfurterClient,
    ReferenceRate,
)
from app.main import app
from app.database import Base, get_db
from app.models.user import User
from app.models.ledger_entry import LedgerEntry
from app.models.transaction import Transaction
from app.models.wallet import Wallet
from app.repositories.wallet_repository import WalletRepository
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
    def test_empty_portfolio_is_zero(self):
        result = calculate_wallet_summary([], Currency.AED, {})
        self.assertEqual(result.estimated_total, Decimal("0.00"))
        self.assertEqual(result.wallets, [])
        self.assertFalse(result.is_estimate)

    def test_invalid_or_uncomputable_rates_do_not_produce_a_total(self):
        for value in ("NaN", "Infinity", "0", "-1", "1e-1000000"):
            with self.assertRaises(ExchangeRateUnavailable):
                calculate_wallet_summary(
                    [wallet(1, Currency.USD, "100")], Currency.AED,
                    {Currency.USD: ReferenceRate(Decimal(value), date.today())},
                )

    def test_overall_rate_date_is_the_oldest_used(self):
        older, newer = date.today() - timedelta(days=1), date.today()
        result = calculate_wallet_summary(
            [wallet(1, Currency.USD, "1"), wallet(2, Currency.EUR, "1")], Currency.AED,
            {Currency.USD: ReferenceRate(Decimal("0.25"), older),
             Currency.EUR: ReferenceRate(Decimal("0.5"), newer)},
        )
        self.assertEqual(result.rates_as_of, older)

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
        # Inspect the wallet router directly instead of relying on FastAPI application internals.
        paths = [r.path for r in wallet_router.routes]
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

    async def test_invalid_rows_are_rejected_without_caching(self):
        today = date.today().isoformat()
        valid = {"date": today, "base": "AED", "quote": "USD", "rate": "0.25"}
        eur = {**valid, "quote": "EUR", "rate": "0.5"}
        cases = [
            [{**valid, "rate": value}, eur] for value in (0, -1, True, "NaN", "Infinity", None)
        ] + [
            [{**valid, "date": (date.today() + timedelta(days=1)).isoformat()}, eur],
            [{**valid, "date": "not-a-date"}, eur],
            [{**valid, "base": "USD"}, eur], [valid, valid, eur],
            [{**valid, "quote": "SAR"}, eur], {"rates": [valid, eur]},
        ]
        for rows in cases:
            cache = MemoryCache()
            async with self.make_http(rows) as http:
                with self.assertRaises(ExchangeRateUnavailable):
                    await FrankfurterClient(cache, http).get_rates(Currency.AED, {Currency.USD, Currency.EUR})
            self.assertEqual(cache.items, {})

    async def test_bad_or_stale_cache_is_refetched(self):
        today = date.today().isoformat()
        rows = [{"date": today, "base": "AED", "quote": quote, "rate": "0.25"}
                for quote in ("USD", "EUR")]
        key = "fx:frankfurter:v2:AED:EUR,USD"
        for cached in ("broken json", json.dumps([{**row, "date": "2000-01-01"} for row in rows])):
            cache = MemoryCache()
            cache.items[key] = cached
            async with self.make_http(rows) as http:
                rates = await FrankfurterClient(cache, http).get_rates(Currency.AED, {Currency.USD, Currency.EUR})
            self.assertEqual(set(rates), {Currency.USD, Currency.EUR})
            self.assertEqual(self.calls, 1)
            self.assertEqual(cache.ttls, [21600])

    async def test_redis_cache_failure_still_allows_valid_public_fx(self):
        today = date.today().isoformat()
        rows = [{"date": today, "base": "AED", "quote": quote, "rate": "0.25"}
                for quote in ("USD", "EUR")]
        cache = SimpleNamespace(get=AsyncMock(side_effect=RedisConnectionError()),
                                set=AsyncMock(side_effect=RedisConnectionError()))
        async with self.make_http(rows) as http:
            rates = await FrankfurterClient(cache, http).get_rates(Currency.AED, {Currency.USD, Currency.EUR})
        self.assertEqual(len(rates), 2)

    async def test_provider_errors_and_timeout_fail_closed(self):
        for kind in ("timeout", "http_error", "invalid_json"):
            def handler(request):
                if kind == "timeout":
                    raise httpx.ReadTimeout("provider private detail", request=request)
                return httpx.Response(503 if kind == "http_error" else 200, text="not json")

            cache = MemoryCache()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with self.assertRaises(ExchangeRateUnavailable):
                    await FrankfurterClient(cache, http).get_rates(Currency.AED, {Currency.USD})
            self.assertEqual(cache.items, {})

    async def test_provider_decimal_precision_is_preserved(self):
        exact = "0.272294862000123456789"
        payload = '[{"date":"' + date.today().isoformat() + '","base":"AED","quote":"USD","rate":' + exact + '}]'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=payload))) as http:
            rates = await FrankfurterClient(MemoryCache(), http).get_rates(Currency.AED, {Currency.USD})
        self.assertEqual(rates[Currency.USD].quote_per_base, Decimal(exact))


class WalletSummaryRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        for uid in (1, 2):
            self.db.add(User(id=uid, username=f"User {uid}", email=f"user{uid}@example.com",
                             mobile=f"1234567{uid}", date_of_birth=date(2000, 1, 1), password_hash="unused"))
        self.db.flush()
        self.db.add_all([
            wallet(1, Currency.AED, "100.1234"),
            Wallet(id=2, user_id=2, currency=Currency.USD, balance=Decimal("999")),
        ])
        self.db.commit()
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.fx = AsyncMock()
        self.fx.get_rates.return_value = {
            Currency.USD: ReferenceRate(Decimal("0.25"), date.today()),
            Currency.EUR: ReferenceRate(Decimal("0.50"), date.today()),
        }
        self.patcher = patch("app.api.routes.wallet.FrankfurterClient", return_value=self.fx)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.client.aclose()
        app.dependency_overrides = self.old_overrides
        self.db.close()
        self.engine.dispose()

    def add_foreign_wallets(self):
        self.db.add_all([wallet(3, Currency.USD, "100"),
                         wallet(4, Currency.EUR, "50", WalletStatus.SUSPENDED)])
        self.db.commit()

    async def test_aed_only_returns_200_and_does_not_request_exchange_rates(self):
        response = await self.client.get("/wallets/summary")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["estimated_total"], "100.12")
        self.assertEqual(data["base_currency"], "AED")
        self.assertEqual([w["wallet_id"] for w in data["wallets"]], [1])
        self.assertFalse(data["is_estimate"])
        self.fx.get_rates.assert_not_awaited()

    async def test_mixed_currencies_return_full_total_without_changing_money(self):
        self.add_foreign_wallets()
        statement = select(Wallet.id, Wallet.balance, Wallet.status).order_by(Wallet.id)
        before = self.db.execute(statement).all()
        response = await self.client.get("/wallets/summary?base_currency=AED")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["estimated_total"], "600.12")
        self.assertEqual(len(data["wallets"]), 3)
        self.assertEqual(data["rate_source"], "Frankfurter (reference rates)")
        self.assertEqual(self.db.execute(statement).all(), before)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(LedgerEntry)), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Transaction)), 0)
        self.fx.get_rates.assert_awaited_once_with(Currency.AED, {Currency.USD, Currency.EUR})

    async def test_fx_outage_or_incomplete_rates_return_503_not_partial_total(self):
        self.add_foreign_wallets()
        for failure in (True, False):
            self.fx.get_rates.side_effect = ExchangeRateUnavailable("private provider detail") if failure else None
            self.fx.get_rates.return_value = {}
            response = await self.client.get("/wallets/summary")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": "Reference exchange rates are temporarily unavailable"})

    async def test_empty_user_has_zero_without_fx(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=999)
        response = await self.client.get("/wallets/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["estimated_total"], "0.00")
        self.assertEqual(response.json()["wallets"], [])
        self.fx.get_rates.assert_not_awaited()

    async def test_base_currency_is_validated_and_can_be_changed(self):
        self.fx.get_rates.return_value = {Currency.AED: ReferenceRate(Decimal("4"), date.today())}
        response = await self.client.get("/wallets/summary?base_currency=USD")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["base_currency"], "USD")
        self.assertEqual(response.json()["estimated_total"], "25.03")
        response = await self.client.get("/wallets/summary?base_currency=SAR")
        self.assertEqual(response.status_code, 422)

    async def test_authentication_is_required(self):
        del app.dependency_overrides[get_current_user]
        response = await self.client.get("/wallets/summary")
        self.assertEqual(response.status_code, 401)
        self.fx.get_rates.assert_not_awaited()

    async def test_database_lookup_uses_worker_and_queries_only_current_user(self):
        original = WalletRepository.get_by_user_id
        loop_thread = threading.get_ident()

        def read(repository, uid):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            self.assertEqual(uid, 1)
            return original(repository, uid)

        with patch.object(WalletRepository, "get_by_user_id", read):
            response = await self.client.get("/wallets/summary")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
