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

    def get_by_user_id(self, user_id: int) -> list[Wallet]:
        stmt = select(Wallet).where(
            Wallet.user_id == user_id
        )

        return list(self.db.scalars(stmt).all())

    def get_by_id_and_user(
        self,
        wallet_id: int,
        user_id: int,
    ) -> Wallet | None:
        stmt = select(Wallet).where(
            Wallet.id == wallet_id,
            # ! important for security, we need to make sure that the wallet belongs to the user, otherwise the user can access other user's wallets
            Wallet.user_id == user_id,
        )

        return self.db.scalar(stmt)

    def get_wallets_for_update(
        self,
        wallet_ids: list[int],
    ) -> list[Wallet]:
        """Load and lock existing wallets in ascending id order.

        Why use the same lock order for every transfer?
            Without a shared order, opposite transfers can deadlock:
                Transfer 1: locks 10 -> waits for 20
                Transfer 2: locks 20 -> waits for 10

            With ORDER BY id, both transfers request locks as 10 -> 20:
                Transfer 1: locks 10 -> locks 20 -> commits
                Transfer 2: waits at 10 -> then locks 10 and 20

            The waiting transfer does not lock 20 first. Consistent ordering
            reduces deadlock risk when all callers follow the same protocol;
            other queries or locks can still introduce deadlocks.

        Why return a list?
            This query can match multiple wallets, so scalars() reads all
            matching Wallet objects rather than scalar() returning only one.
            all() collects the results; list() makes the declared list[Wallet]
            return type explicit. It does not copy or detach the ORM objects.

            Results follow id order, not sender/receiver or input order.
            The caller must identify each wallet by id and check that both
            requested wallets exist. Missing ids produce no placeholder;
            repeated input ids do not produce duplicate result rows.

        Locks are acquired when the query executes and held until the current
        database transaction commits or rolls back. The caller must keep the
        checks and balance changes inside that transaction.
        """
        stmt = (
            select(Wallet)
            .where(Wallet.id.in_(wallet_ids))
            .order_by(Wallet.id)  # Lock wallets in a shared, predictable order.
            .with_for_update()  # Protect balance checks and updates until  commit/rollback. this lock is released when the transaction is committed or rolled back, so we need to make sure that we commit or rollback the transaction after we are done with the wallets.
        )

        return list(self.db.scalars(stmt).all())
