from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import Currency
from app.models.topup import TopUpStatus


class CreateTopUpRequest(BaseModel):
    wallet_id: int
    amount: Decimal = Field(
        gt=0,
        max_digits=14,
        decimal_places=2,
    )


class TopUpResponse(BaseModel):
    id: UUID
    wallet_id: int
    amount_minor: int
    currency: Currency
    moyasar_payment_id: str
    status: TopUpStatus

    model_config = ConfigDict(from_attributes=True)