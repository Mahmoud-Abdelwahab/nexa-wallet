from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.core.enums import Currency, TransactionStatus


class TransactionResponse(BaseModel):
    id: int
    reference: str
    sender_wallet_id: int | None
    receiver_wallet_id: int | None
    amount: Decimal
    currency: Currency
    status: TransactionStatus
    created_at: datetime

    model_config = {
        "from_attributes": True,
    }