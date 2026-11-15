"""Read-only estimated portfolio total, not a transferable balance."""
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from app.core.enums import Currency, WalletStatus


class WalletConversionResponse(BaseModel):
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
    rates_as_of: date | None
    wallets: list[WalletConversionResponse]
