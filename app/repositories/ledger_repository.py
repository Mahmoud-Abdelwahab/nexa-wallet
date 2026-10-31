from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ledger_entry import LedgerEntry


class LedgerRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, entry: LedgerEntry) -> LedgerEntry:
        # Add the entry; the transfer service controls commit and rollback.
        self.db.add(entry)
        return entry

    def get_by_wallet_id(
        self,
        wallet_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> list[LedgerEntry]:
        stmt = (
            select(LedgerEntry)
            # Select entries for the wallet already authorized by the service.
            .where(LedgerEntry.wallet_id == wallet_id)
            # Newest first; id breaks ties when timestamps match.
            .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
            # Return one page, skipping entries from earlier pages.
            .limit(limit)
            .offset(offset)
        )

        # Execute the query and collect LedgerEntry objects.
        return list(self.db.scalars(stmt).all())
