from pydantic import BaseModel

from app.models.wallet import Currency
from decimal import Decimal
from app.models.wallet import Currency, WalletStatus


class CreateWalletRequest(BaseModel):
    currency: Currency



class WalletResponse(BaseModel):
    id: int
    currency: Currency
    balance: Decimal
    status: WalletStatus

    model_config = {
        "from_attributes": True, # This allows the model to be created from an ORM object, such as a SQLAlchemy model. It tells Pydantic to read the attributes of the object and use them to populate the fields of the Pydantic model.
    }