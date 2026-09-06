from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.wallet import Wallet


class WalletRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, wallet: Wallet) -> Wallet:
        self.db.add(wallet)
        return wallet

    def get_by_id(
        self,
        wallet_id: int,
    ) -> Wallet | None:
        return self.db.get(Wallet, wallet_id)

    def get_by_user_and_currency(
        self,
        user_id: int,
        currency,
    ) -> Wallet | None:

        stmt = select(Wallet).where(  # select(Wallet) return array of Wallet objects, we use scalar() to get the first one
            Wallet.user_id == user_id,
            Wallet.currency == currency,
        )

        return self.db.scalar(stmt)