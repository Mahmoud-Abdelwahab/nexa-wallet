from pydantic import BaseModel, Field


class RecipientLookupRequest(BaseModel):
    sender_wallet_id: int = Field(gt=0)
    recipient_wallet_id: int = Field(gt=0)


class RecipientLookupResponse(BaseModel):
    wallet_id: int
    display_name: str
    currency: str
