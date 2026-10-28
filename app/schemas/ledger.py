from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.core.enums import Currency
from app.models.ledger_entry import (
    LedgerDirection,
    LedgerEntryType,
)


class LedgerEntryResponse(BaseModel):
    id: int
    transaction_id: int
    direction: LedgerDirection
    entry_type: LedgerEntryType
    amount: Decimal
    currency: Currency
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)