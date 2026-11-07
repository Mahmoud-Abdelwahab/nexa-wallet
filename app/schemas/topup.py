from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import Currency
from app.models.topup import TopUpStatus


# The mobile sends these fields to start a funding request.
class CreateTopUpRequest(BaseModel):
    """Input schema, not a database table.

    Source: the mobile's POST /topups JSON body (wallet_id and amount).
    Use: FastAPI validates it before passing the request to TopUpService.
    """

    wallet_id: int
    amount: Decimal = Field(
        gt=0,
        max_digits=14,
        decimal_places=2,
    )


# The backend returns these fields so the mobile can start payment or check funding status.
class TopUpResponse(BaseModel):
    """Output schema, not a database table.

    Source: a saved TopUp object, read using from_attributes=True.
    Use: POST /topups and GET /topups/{id} return it to the mobile.
    Its id is Nexa's TopUp ID; moyasar_payment_id is the SDK's givenID.
    """

    id: UUID
    wallet_id: int
    amount_minor: int
    currency: Currency
    moyasar_payment_id: str
    status: TopUpStatus

    model_config = ConfigDict(from_attributes=True)
