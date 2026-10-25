from sqlalchemy.orm import Session

from app.models.wallet import Currency, Wallet
from app.repositories.wallet_repository import WalletRepository


class WalletService:

    def __init__(self, db: Session):
        self.db = db
        self.wallet_repository = WalletRepository(db)

    def create_wallet(
        self,
        user_id: int,
        currency: Currency,
    ) -> Wallet:

        existing_wallet = (
            self.wallet_repository.get_by_user_and_currency(
                user_id=user_id,
                currency=currency,
            )
        )

        if existing_wallet:
            raise ValueError(
                f"{currency.value} wallet already exists"
            )

        wallet = Wallet(
            user_id=user_id,
            currency=currency,
        )

        self.wallet_repository.create(wallet)

        self.db.commit()

        return wallet

    def get_user_wallets(self, user_id: int) -> list[Wallet]:
        return self.wallet_repository.get_by_user_id(user_id)


    def get_wallet(
        self,
        wallet_id: int,
        user_id: int,
    ) -> Wallet:

        wallet = self.wallet_repository.get_by_id_and_user(
            wallet_id=wallet_id,
            user_id=user_id,
        )

        if wallet is None:
            raise ValueError("Wallet not found") #! not returning  --> You don't own this wallet for seurity reasonse - to not disclose if the wallet exists or not, we just say not found

        return wallet