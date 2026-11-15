"""Pure conversion logic. No balance updates, transfers or ledger writes."""
from decimal import Decimal, ROUND_HALF_UP

from app.core.enums import Currency
from app.integrations.frankfurter_client import ExchangeRateUnavailable, ReferenceRate
from app.models.wallet import Wallet
from app.schemas.wallet_summary import WalletConversionResponse, WalletSummaryResponse


DISPLAY_MINOR_UNIT = Decimal("0.01")
DISPLAY_RATE_PRECISION = Decimal("0.000000000001")


def calculate_wallet_summary(
    wallets: list[Wallet],
    base: Currency,
    reference_rates: dict[Currency, ReferenceRate],
) -> WalletSummaryResponse:
    lines: list[WalletConversionResponse] = []
    rate_dates = []

    # Do not drop suspended/inactive wallets: they still represent held money.
    for wallet in wallets:
        amount = wallet.balance
        if wallet.currency == base:
            multiplier = Decimal("1")
            rate_date = None
        else:
            rate = reference_rates.get(wallet.currency)
            if rate is None or rate.quote_per_base <= 0:
                # Never return a misleading partial total.
                raise ExchangeRateUnavailable()
            # Frankfurter queried with BASE=target, QUOTE=source.
            # Example: 1 AED = 0.27 USD -> 1 USD = 1/0.27 AED.
            multiplier = Decimal("1") / rate.quote_per_base
            rate_date = rate.published_on
            rate_dates.append(rate_date)

        converted = (amount * multiplier).quantize(
            DISPLAY_MINOR_UNIT, rounding=ROUND_HALF_UP,
        )
        lines.append(WalletConversionResponse(
            wallet_id=wallet.id,
            currency=wallet.currency,
            status=wallet.status,
            balance=amount,
            rate_to_base=multiplier.quantize(
                DISPLAY_RATE_PRECISION, rounding=ROUND_HALF_UP,
            ),
            estimated_in_base=converted,
            rate_date=rate_date,
        ))

    # Sum the displayed rounded lines so UI breakdown equals UI total.
    total = sum((line.estimated_in_base for line in lines), Decimal("0.00"))

    return WalletSummaryResponse(
        base_currency=base,
        estimated_total=total,
        is_estimate=bool(rate_dates),
        rate_source="Frankfurter (reference rates)" if rate_dates else None,
        rates_as_of=min(rate_dates) if rate_dates else None,
        wallets=lines,
    )
