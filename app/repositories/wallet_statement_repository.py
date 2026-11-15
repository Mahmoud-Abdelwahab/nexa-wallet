"""Read-only bounded ledger export. One SELECT sees a consistent statement snapshot."""
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ledger_entry import LedgerEntry
from app.models.transaction import Transaction


MAX_STATEMENT_ROWS = 5000


class WalletStatementRepository:
    def __init__(self, db: Session):
        self.db = db

    def fetch_rows(
        self,
        wallet_id: int,
        start_inclusive: datetime,
        end_exclusive: datetime,
    ) -> list[tuple[LedgerEntry, str]]:
        # One extra row detects overflow. Do not silently truncate statements.
        query = (
            select(LedgerEntry, Transaction.reference)
            .join(Transaction, LedgerEntry.transaction_id == Transaction.id)
            .where(
                LedgerEntry.wallet_id == wallet_id,
                LedgerEntry.created_at >= start_inclusive,
                LedgerEntry.created_at < end_exclusive,
            )
            .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
            .limit(MAX_STATEMENT_ROWS + 1)
        )
        return [(entry, reference) for entry, reference in self.db.execute(query).all()]
