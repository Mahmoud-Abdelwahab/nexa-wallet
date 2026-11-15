"""Wallet CSV statement contract tests (SQLite, no external services)."""
import csv
import io
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.dependencies import get_current_user
from app.core.enums import Currency, TransactionStatus, WalletStatus
from app.database import Base, get_db
from app.main import app
from app.models import User, Wallet, Transaction, LedgerEntry
from app.models.ledger_entry import LedgerDirection, LedgerEntryType
from app.repositories.wallet_statement_repository import MAX_STATEMENT_ROWS
from app.services.wallet_statement_service import render_statement_csv


class StatementExportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

        self.user = User(
            username="statement-owner",
            email="statement-owner@example.com",
            mobile="88000001",
            password_hash="unused",
            date_of_birth=date(1990, 1, 1),
        )
        other = User(
            username="other",
            email="other@example.com",
            mobile="88000002",
            password_hash="unused",
            date_of_birth=date(1990, 1, 1),
        )
        self.db.add_all([self.user, other])
        self.db.flush()

        self.wallet = Wallet(
            user_id=self.user.id,
            currency=Currency.AED,
            balance=Decimal("125.0000"),
            status=WalletStatus.ACTIVE,
        )
        self.other_wallet = Wallet(
            user_id=other.id,
            currency=Currency.AED,
            balance=Decimal("999.0000"),
            status=WalletStatus.ACTIVE,
        )
        self.db.add_all([self.wallet, self.other_wallet])
        self.db.commit()

        self.original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

        self.addCleanup(self.client.close)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        app.dependency_overrides = self.original_overrides
        self.db.close()
        self.engine.dispose()

    def _entry(self, wallet, day, direction, amount, reference):
        transaction = Transaction(
            reference=reference,
            sender_wallet_id=None,
            receiver_wallet_id=wallet.id,
            amount=Decimal(amount),
            currency=Currency.AED,
            status=TransactionStatus.SUCCESS,
        )
        self.db.add(transaction)
        self.db.flush()

        entry = LedgerEntry(
            transaction_id=transaction.id,
            wallet_id=wallet.id,
            direction=direction,
            entry_type=LedgerEntryType.TOP_UP,
            amount=Decimal(amount),
            currency=Currency.AED,
            created_at=datetime.combine(day, time(12, 0, 0)),
        )
        self.db.add(entry)
        self.db.commit()
        return entry

    @staticmethod
    def _csv_rows(response):
        # utf-8-sig intentionally strips the Excel-friendly UTF-8 BOM.
        return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_download_has_correct_headers_signed_amount_and_no_sensitive_other_wallet_data(self):
        today = date.today()
        self._entry(self.wallet, today, LedgerDirection.CREDIT, "120.0000", "topup-1")
        self._entry(self.wallet, today, LedgerDirection.DEBIT, "20.0000", "transfer-1")
        self._entry(self.other_wallet, today, LedgerDirection.CREDIT, "999.0000", "not-owned")
        old_balance = self.wallet.balance

        response = self.client.get(f"/wallets/{self.wallet.id}/statement")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("attachment;", response.headers["content-disposition"])
        self.assertIn(".csv", response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")

        rows = self._csv_rows(response)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [r["transaction_reference"] for r in rows],
            ["topup-1", "transfer-1"],
        )
        self.assertEqual(
            [r["signed_amount"] for r in rows],
            ["120.0000", "-20.0000"],
        )
        self.assertEqual([r["currency"] for r in rows], ["AED", "AED"])
        self.assertNotIn("not-owned", response.text)
        self.assertEqual(self.wallet.balance, old_balance)

    def test_date_window_is_inclusive_for_both_calendar_days(self):
        today = date.today()
        day_before = today - timedelta(days=1)
        three_days_ago = today - timedelta(days=3)
        self._entry(self.wallet, three_days_ago, LedgerDirection.CREDIT, "5", "outside")
        self._entry(self.wallet, day_before, LedgerDirection.CREDIT, "10", "first")
        self._entry(self.wallet, today, LedgerDirection.CREDIT, "20", "last")

        response = self.client.get(
            f"/wallets/{self.wallet.id}/statement",
            params={
                "start_date": day_before.isoformat(),
                "end_date": today.isoformat(),
                "format": "csv",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        rows = self._csv_rows(response)
        self.assertEqual(
            [r["transaction_reference"] for r in rows],
            ["first", "last"],
        )

    def test_wallet_ownership_returns_indistinguishable_404(self):
        existing = self.client.get(f"/wallets/{self.other_wallet.id}/statement")
        missing = self.client.get("/wallets/123456789/statement")
        self.assertEqual(existing.status_code, 404)
        self.assertEqual(existing.json(), missing.json())
        self.assertEqual(existing.json(), {"detail": "Wallet not found"})

    def test_missing_auth_returns_401(self):
        app.dependency_overrides.pop(get_current_user)
        response = self.client.get(f"/wallets/{self.wallet.id}/statement")
        self.assertEqual(response.status_code, 401)

    def test_date_range_format_and_row_limit_are_bounded(self):
        today = date.today()
        future = today + timedelta(days=1)
        old = today - timedelta(days=366)
        wrong = [
            {"start_date": future.isoformat(), "end_date": today.isoformat()},
            {"end_date": future.isoformat()},
            {"start_date": old.isoformat(), "end_date": today.isoformat()},
        ]
        for params in wrong:
            with self.subTest(params=params):
                response = self.client.get(
                    f"/wallets/{self.wallet.id}/statement", params=params,
                )
                self.assertEqual(response.status_code, 400, response.text)

        invalid_format = self.client.get(
            f"/wallets/{self.wallet.id}/statement", params={"format": "pdf"},
        )
        self.assertEqual(invalid_format.status_code, 422)

        with patch(
            "app.services.wallet_statement_service.WalletStatementRepository.fetch_rows",
            return_value=[(None, "")] * (MAX_STATEMENT_ROWS + 1),
        ):
            response = self.client.get(f"/wallets/{self.wallet.id}/statement")
        self.assertEqual(response.status_code, 413)

    def test_empty_statement_returns_csv_headers(self):
        response = self.client.get(f"/wallets/{self.wallet.id}/statement")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._csv_rows(response), [])
        self.assertIn("ledger_entry_id", response.content.decode("utf-8-sig"))

    def test_formula_like_reference_is_safe_for_spreadsheets(self):
        entry = SimpleNamespace(
            created_at=datetime(2026, 11, 10, 8),
            id=1,
            transaction_id=10,
            entry_type=LedgerEntryType.TRANSFER,
            direction=LedgerDirection.CREDIT,
            amount=Decimal("1.00"),
            currency=Currency.AED,
        )
        result = render_statement_csv([(entry, '=HYPERLINK("https://example.com")')])
        rows = list(csv.DictReader(io.StringIO(result)))
        self.assertTrue(rows[0]["transaction_reference"].startswith("'="))


if __name__ == "__main__":
    unittest.main()
