from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.transaction import Transaction
from app.models.wallet import Wallet


class TransactionRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, transaction: Transaction) -> Transaction:
        self.db.add(transaction)
        # Send the INSERT to obtain the id needed by ledger entries, without
        # committing. The service commits or rolls back the whole operation.
        self.db.flush()

        return transaction

    def get_by_id(
        self,
        transaction_id: int,
    ) -> Transaction | None:
        # Look up one transaction by its primary key. This does not check
        # ownership; the caller must authorize access before returning it.
        return self.db.get(Transaction, transaction_id)

    def get_by_user_id(
        self,
        user_id: int,
    ) -> list[Transaction]:
        # Build a subquery selecting ids of wallets owned by this user.
        # It is embedded in the main SQL query, not executed separately here.
        wallet_ids = select(Wallet.id).where(Wallet.user_id == user_id)

        stmt = (
            select(Transaction)
            .where(
                # or_() builds SQL OR: include a transaction if at least one
                # condition is true. in_(wallet_ids) checks whether the wallet
                # id belongs to the set selected by the user's wallet subquery.
                #
                # Example: the user's wallet ids are [1, 5]:
                #   sender=1, receiver=3 -> include: sender is their wallet.
                #   sender=8, receiver=5 -> include: receiver is their wallet.
                #   sender=7, receiver=9 -> exclude: neither wallet is theirs.
                #   sender=1, receiver=5 -> include once: both conditions match.
                #
                # SQLAlchemy calls it or_ because "or" is a Python keyword.
                # The database filters the rows; Python does not load the
                # entire table and loop over it. AND would require both sides
                # to belong to the user, omitting transfers to/from others.
                or_(
                    Transaction.sender_wallet_id.in_(wallet_ids),
                    Transaction.receiver_wallet_id.in_(wallet_ids),
                )
            )
            # Show the newest transactions first, regardless of their status.
            .order_by(Transaction.created_at.desc())
        )

        # Execute the combined query once; scalars() returns Transaction ORM
        # objects and all() collects every match. No matches gives an empty list.
        return list(self.db.scalars(stmt).all())

    def get_by_id_for_user(
        self,
        transaction_id: int,
        user_id: int,
    ) -> Transaction | None:
        # * Select wallets owned by the authenticated user, not a client-supplied
        # owner. The route must obtain user_id from current_user.
        wallet_ids = select(Wallet.id).where(Wallet.user_id == user_id)

        stmt = (
            select(Transaction)
            .where(
                # Both requirements must hold: the transaction id matches AND
                # the user owns at least one participating wallet (sender OR
                # receiver). A matching id alone must never grant access.
                Transaction.id == transaction_id,
                or_(
                    Transaction.sender_wallet_id.in_(wallet_ids),
                    Transaction.receiver_wallet_id.in_(wallet_ids),
                ),
            )
        )

        # Return None for both missing and unauthorized transactions. The
        # service can then raise one generic not-found error without revealing
        # whether another user's transaction exists.
        return self.db.scalar(stmt)
