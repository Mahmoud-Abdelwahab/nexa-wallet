from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import WalletStatus
from app.models.user import User
from app.models.wallet import Wallet
from app.schemas.recipient import RecipientLookupResponse


def mask_name(full_name: str) -> str:
    parts = full_name.strip().split()
    if not parts:
        return "Nexa Wallet user"
    if len(parts) == 1:
        return parts[0][0] + "***"
    return f"{parts[0]} {parts[-1][0]}."


class RecipientService:
    def __init__(self, db: Session):
        self.db = db

    def resolve(
        self,
        current_user_id: int,
        sender_wallet_id: int,
        recipient_wallet_id: int,
    ) -> RecipientLookupResponse:
        sender = self.db.scalar(
            select(Wallet).where(
                Wallet.id == sender_wallet_id,
                Wallet.user_id == current_user_id,
            )
        )
        if sender is None:
            raise HTTPException(404, "Sender wallet not found")

        if sender_wallet_id == recipient_wallet_id:
            raise HTTPException(400, "Cannot transfer to the same wallet")

        recipient = self.db.execute(
            select(Wallet.id, User.username, Wallet.currency)
            .join(User, User.id == Wallet.user_id)
            .where(
                Wallet.id == recipient_wallet_id,
                Wallet.status == WalletStatus.ACTIVE,
                Wallet.currency == sender.currency,
            )
        ).first()
        # The same response covers missing, inactive and different-currency
        # wallets without exposing the recipient's other details.
        if recipient is None:
            raise HTTPException(404, "Recipient wallet not found")

        wallet_id, username, currency = recipient
        return RecipientLookupResponse(
            wallet_id=wallet_id,
            display_name=mask_name(username),
            currency=currency.value,
        )
