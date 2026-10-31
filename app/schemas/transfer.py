from decimal import Decimal

from pydantic import BaseModel, Field

from app.core.enums import Currency, TransactionStatus


class TransferRequest(BaseModel):
    sender_wallet_id: int
    receiver_wallet_id: int
    amount: Decimal = Field(gt=0)
    message: str | None = None


class TransferResponse(BaseModel):
    id: int
    reference: str
    sender_wallet_id: int
    receiver_wallet_id: int
    amount: Decimal
    currency: Currency
    status: TransactionStatus
    gateway_message: str | None = None

    model_config = {
        "from_attributes": True,
    }