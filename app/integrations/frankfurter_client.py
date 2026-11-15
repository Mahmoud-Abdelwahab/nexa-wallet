"""Frankfurter v2 reference FX rates, with a best-effort Redis cache.

Rates are for DISPLAY ONLY. Never use them to settle a transfer or post ledger
entries. The API's rate is 'quote currency per 1 base currency'.
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol

import httpx

from app.core.enums import Currency


FRANKFURTER_RATES_URL = "https://api.frankfurter.dev/v2/rates"
FX_CACHE_SECONDS = 6 * 60 * 60
MAX_RATE_AGE_DAYS = 7


class ExchangeRateUnavailable(Exception):
    """An external reference rate is missing, malformed or too old."""


@dataclass(frozen=True)
class ReferenceRate:
    # Number of quote-currency units in ONE base-currency unit.
    quote_per_base: Decimal
    published_on: date


class RatesCache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ex: int) -> object: ...


class FrankfurterClient:
    def __init__(
        self,
        redis: RatesCache,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.redis = redis
        self.http_client = http_client

    @staticmethod
    def _validate_rows(
        rows: object,
        base: Currency,
        quotes: set[Currency],
    ) -> dict[Currency, ReferenceRate]:
        if not isinstance(rows, list):
            raise ExchangeRateUnavailable()

        result: dict[Currency, ReferenceRate] = {}
        today = datetime.now(timezone.utc).date()

        for row in rows:
            if not isinstance(row, dict) or row.get("base") != base.value:
                raise ExchangeRateUnavailable()

            try:
                quote = Currency(row["quote"])
                value = row["rate"]
                if isinstance(value, bool):
                    raise ValueError("rate must be numeric")
                rate = Decimal(str(value))
                published = date.fromisoformat(row["date"])
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                raise ExchangeRateUnavailable() from exc

            age = (today - published).days
            if (
                quote not in quotes
                or quote in result
                or not rate.is_finite()
                or rate <= 0
                or age < 0
                or age > MAX_RATE_AGE_DAYS
            ):
                raise ExchangeRateUnavailable()

            result[quote] = ReferenceRate(
                quote_per_base=rate,
                published_on=published,
            )

        if set(result) != quotes:
            raise ExchangeRateUnavailable()
        return result

    async def get_rates(
        self,
        base: Currency,
        quotes: set[Currency],
    ) -> dict[Currency, ReferenceRate]:
        # Same-currency balances need no external dependency.
        quotes = quotes - {base}
        if not quotes:
            return {}

        ordered_quotes = sorted(q.value for q in quotes)
        key = f"fx:frankfurter:v2:{base.value}:{','.join(ordered_quotes)}"

        # Cache outage must not stop an otherwise healthy FX lookup.
        try:
            cached = await self.redis.get(key)
            if cached:
                try:
                    return self._validate_rows(
                        json.loads(cached), base, quotes,
                    )
                except (ValueError, TypeError, ExchangeRateUnavailable):
                    pass
        except Exception:
            pass

        params = {
            "base": base.value,
            "quotes": ",".join(ordered_quotes),
        }
        try:
            if self.http_client is None:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(FRANKFURTER_RATES_URL, params=params)
            else:
                response = await self.http_client.get(
                    FRANKFURTER_RATES_URL, params=params,
                )
            response.raise_for_status()

            # Avoid float roundoff when parsing published numeric rates.
            rows = json.loads(response.text, parse_float=Decimal)
            rates = self._validate_rows(rows, base, quotes)
        except (httpx.HTTPError, ValueError, TypeError, ExchangeRateUnavailable) as exc:
            raise ExchangeRateUnavailable() from exc

        # Redis stores only public FX data, never wallet balances/user data.
        try:
            await self.redis.set(
                key,
                json.dumps([
                    {
                        "base": base.value,
                        "quote": quote.value,
                        "rate": str(item.quote_per_base),
                        "date": item.published_on.isoformat(),
                    }
                    for quote, item in sorted(rates.items(), key=lambda x: x[0].value)
                ]),
                ex=FX_CACHE_SECONDS,
            )
        except Exception:
            pass

        return rates
