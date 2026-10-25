from sqlalchemy.orm import Session

from app.models.ledger_entry import LedgerEntry


class LedgerRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, entry: LedgerEntry) -> LedgerEntry:
        self.db.add(entry)
        return entry

    # TransferService which will decide whether to commit or rollback the transaction, so we don't commit here.