from sqlalchemy.orm import Session

from app.models.wallet import Wallet


class WalletRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, wallet: Wallet) -> Wallet:
        self.db.add(wallet)
        return wallet