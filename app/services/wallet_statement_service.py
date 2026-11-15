"""Export wallet ledger activity as an RFC 4180-style CSV for the user.

This is NOT a certified bank statement. It contains posted ledger movements,
not an independently reconciled opening/closing balance.
"""
import csv
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.models.ledger_entry import LedgerDirection, LedgerEntry
from app.repositories.wallet_repository import WalletRepository
from app.repositories.wallet_statement_repository import (
    MAX_STATEMENT_ROWS,
    WalletStatementRepository,
)


class InvalidStatementPeriod(Exception):
    pass


class StatementTooLarge(Exception):
    pass


CSV_HEADERS = [
    "created_at",
    "ledger_entry_id",
    "transaction_id",
    "transaction_reference",
    "entry_type",
    "direction",
    "amount",
    "currency",
    "signed_amount",
]


def _safe_csv_text(value: str) -> str:
    # Spreadsheet programs may execute text values beginning with formulas.
    # Our references are server-generated, but sanitize defensively.
    value = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def render_statement_csv(rows: list[tuple[LedgerEntry, str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)

    for entry, reference in rows:
        amount = entry.amount
        signed = -amount if entry.direction == LedgerDirection.DEBIT else amount
        writer.writerow([
            entry.created_at.isoformat(sep=" "),
            entry.id,
            entry.transaction_id,
            _safe_csv_text(reference),
            entry.entry_type.value,
            entry.direction.value,
            format(amount, ".4f"),
            entry.currency.value,
            format(signed, ".4f"),
        ])

    return output.getvalue()


class WalletStatementService:
    def __init__(self, db: Session):
        self.db = db
        self.wallet_repository = WalletRepository(db)
        self.statement_repository = WalletStatementRepository(db)

    @staticmethod
    def normalize_period(
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[date, date]:
        # A no-parameter export downloads the last 30 calendar days.
        to_day = end_date or date.today()
        try:
            from_day = start_date or (to_day - timedelta(days=29))
        except OverflowError as exc:
            raise InvalidStatementPeriod() from exc

        if (
            from_day > to_day
            or to_day > date.today()
            or (to_day - from_day).days > 365
        ):
            raise InvalidStatementPeriod()
        return from_day, to_day

    def export_csv(
        self,
        user_id: int,
        wallet_id: int,
        from_day: date,
        to_day: date,
    ) -> str:
        wallet = self.wallet_repository.get_by_id_and_user(wallet_id, user_id)
        if wallet is None:
            # Do not disclose existence of other users' wallets.
            raise BusinessError("Wallet not found")

        start = datetime.combine(from_day, time.min)
        end = datetime.combine(to_day + timedelta(days=1), time.min)

        rows = self.statement_repository.fetch_rows(wallet_id, start, end)
        if len(rows) > MAX_STATEMENT_ROWS:
            raise StatementTooLarge()

        # Currency is part of each ledger entry, avoiding implicit FX conversion.
        return render_statement_csv(rows)
