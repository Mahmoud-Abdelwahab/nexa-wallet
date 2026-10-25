from sqlalchemy.orm import Session

from app.models.transaction import Transaction


class TransactionRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, transaction: Transaction) -> Transaction:
        self.db.add(transaction)
        self.db.flush()  # Flush the session to generate the transaction ID As we need to link the trasaction with the ledger entry, we need to flush the session to generate the transaction ID before [ committing the transaction].

        return transaction
    # TransferService which will decide whether to commit or rollback the transaction, so we don't commit here.
    
    def get_by_id(
        self,
        transaction_id: int,
    ) -> Transaction | None:
        return self.db.get(Transaction, transaction_id)