from sqlalchemy.orm import Session

from app.models.transaction import Transaction
from app.repositories.transaction_repository import TransactionRepository


class TransactionService:

    def __init__(self, db: Session):
        self.transaction_repository = TransactionRepository(db)

    def get_user_transactions(
        self,
        user_id: int,
    ) -> list[Transaction]:

        return self.transaction_repository.get_by_user_id(
            user_id
        )

    # Expected detail-endpoint authorization flow:
    # GET /transactions/123
    #          |
    #          v
    # current_user (authenticated by the route)
    #          |
    #          v
    # Does the user own the sender OR receiver wallet?
    #          |
    #          +-- No / transaction missing -> "Transaction not found"
    #          |                              (route should return 404)
    #          +-- Yes -> return the transaction
    #
    # Knowing a transaction id is not permission to view it. Check access
    # before returning any data, and use the same not-found result for missing
    # and unauthorized records so we do not disclose another user's activity.
    def get_transaction(
        self,
        transaction_id: int,
        user_id: int,
    ) -> Transaction:
        transaction = self.transaction_repository.get_by_id_for_user(
            transaction_id=transaction_id,
            user_id=user_id,
        )

        if transaction is None:
            raise ValueError("Transaction not found")

        return transaction
