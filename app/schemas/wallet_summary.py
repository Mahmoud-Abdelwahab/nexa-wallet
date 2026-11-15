"""Read-only estimated portfolio total, not a transferable balance."""
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from app.core.enums import Currency, WalletStatus


class WalletConversionResponse(BaseModel):
    # Keep the original balance in its own currency; estimated_in_base is a separate display value.
    wallet_id: int
    currency: Currency
    status: WalletStatus
    balance: Decimal
    # 1 unit of the wallet currency expressed in the requested base currency.
    rate_to_base: Decimal
    estimated_in_base: Decimal
    rate_date: date | None


class WalletSummaryResponse(BaseModel):
    base_currency: Currency
    estimated_total: Decimal
    is_estimate: bool
    rate_source: str | None
    # Report the oldest rate date used, avoiding a misleading claim that every rate is newer.
    # Without FX conversion: rates_as_of/rate_source are None and is_estimate is False.
    rates_as_of: date | None
    wallets: list[WalletConversionResponse]
