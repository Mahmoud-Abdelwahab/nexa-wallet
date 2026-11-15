"""Wallet CSV statement contract tests (SQLite, no external services)."""
import csv
import io
import unittest
import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.dependencies import get_current_user
from app.core.enums import Currency, TransactionStatus, WalletStatus
from app.database import Base, get_db
from app.main import app
from app.models import User, Wallet, Transaction, LedgerEntry
from app.models.ledger_entry import LedgerDirection, LedgerEntryType
from app.repositories.wallet_statement_repository import MAX_STATEMENT_ROWS, WalletStatementRepository
from app.services.wallet_statement_service import InvalidStatementPeriod, WalletStatementService, render_statement_csv


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

    def test_formula_variants_and_csv_delimiters_stay_single_text_cells(self):
        entry = SimpleNamespace(
            created_at=datetime(2026, 11, 10, 8), id=1, transaction_id=10,
            entry_type=LedgerEntryType.TRANSFER, direction=LedgerDirection.CREDIT,
            amount=Decimal("1"), currency=Currency.AED,
        )
        for reference in ("=1+1", " +1", "-2+3", "@SUM(A1)", "\t=1", "\r\n=1",
                          "＝1+1", "＋1", "－1", "＠SUM(A1)", 'plain,"text"\nnext'):
            rows = list(csv.DictReader(io.StringIO(render_statement_csv([(entry, reference)]))))
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]), 9)
            value = rows[0]["transaction_reference"]
            self.assertNotIn("\n", value)
            self.assertNotIn("\r", value)
            self.assertNotIn("\t", value)
            if not reference.startswith("plain"):
                self.assertTrue(value.startswith("'"), reference)

    def test_exact_5000_downloads_but_5001_is_rejected_without_truncation(self):
        entry = self._entry(self.wallet, date.today(), LedgerDirection.CREDIT, "1", "bulk-export")
        row = {
            "transaction_id": entry.transaction_id, "wallet_id": self.wallet.id,
            "direction": LedgerDirection.CREDIT, "entry_type": LedgerEntryType.TOP_UP,
            "amount": Decimal("1"), "currency": Currency.AED, "created_at": entry.created_at,
        }
        self.db.execute(insert(LedgerEntry), [row.copy() for _ in range(MAX_STATEMENT_ROWS - 1)])
        self.db.commit()
        response = self.client.get(f"/wallets/{self.wallet.id}/statement")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self._csv_rows(response)), MAX_STATEMENT_ROWS)
        self.db.execute(insert(LedgerEntry), [row])
        self.db.commit()
        response = self.client.get(f"/wallets/{self.wallet.id}/statement")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": "Too many entries; choose a shorter date range"})

    def test_default_period_and_366_day_inclusive_limit(self):
        today = date.today()
        self.assertEqual(WalletStatementService.normalize_period(None, None), (today - timedelta(days=29), today))
        self.assertEqual(WalletStatementService.normalize_period(today - timedelta(days=365), today),
                         (today - timedelta(days=365), today))
        with self.assertRaises(InvalidStatementPeriod):
            WalletStatementService.normalize_period(today - timedelta(days=366), today)
        # The default excludes day 31 and starts at midnight, not the request time.
        self._entry(self.wallet, today - timedelta(days=30), LedgerDirection.CREDIT, "1", "old")
        self._entry(self.wallet, today - timedelta(days=29), LedgerDirection.CREDIT, "1", "included")
        response = self.client.get(f"/wallets/{self.wallet.id}/statement")
        self.assertEqual([r["transaction_reference"] for r in self._csv_rows(response)], ["included"])

    def test_end_of_day_is_inclusive_next_midnight_is_excluded_and_ties_use_id(self):
        day = date.today() - timedelta(days=2)
        # Insert the newest movement first to verify chronological ordering independently of insertion order.
        end = self._entry(self.wallet, day, LedgerDirection.CREDIT, "1", "end")
        end.created_at = datetime.combine(day, time.max)
        start = self._entry(self.wallet, day, LedgerDirection.CREDIT, "1", "start")
        start.created_at = datetime.combine(day, time.min)
        next_day = self._entry(self.wallet, day + timedelta(days=1), LedgerDirection.CREDIT, "1", "next")
        next_day.created_at = datetime.combine(day + timedelta(days=1), time.min)
        tie = self._entry(self.wallet, day, LedgerDirection.CREDIT, "1", "tie")
        tie.created_at = end.created_at
        self.db.commit()
        response = self.client.get(f"/wallets/{self.wallet.id}/statement", params={"start_date": str(day), "end_date": str(day)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["transaction_reference"] for r in self._csv_rows(response)], ["start", "end", "tie"])

    def test_export_is_read_only_even_for_suspended_wallet(self):
        self._entry(self.wallet, date.today(), LedgerDirection.CREDIT, "5", "original")
        self.wallet.status = WalletStatus.SUSPENDED
        self.db.commit()
        statement = select(Wallet.id, Wallet.balance, Wallet.status).order_by(Wallet.id)
        before = self.db.execute(statement).all()
        counts = [self.db.scalar(select(func.count()).select_from(model)) for model in (Transaction, LedgerEntry)]
        response = self.client.get(f"/wallets/{self.wallet.id}/statement")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.execute(statement).all(), before)
        self.assertEqual([self.db.scalar(select(func.count()).select_from(model)) for model in (Transaction, LedgerEntry)], counts)

    def test_ownership_is_checked_before_reading_ledger_rows(self):
        with patch.object(WalletStatementRepository, "fetch_rows") as fetch:
            response = self.client.get(f"/wallets/{self.other_wallet.id}/statement")
        self.assertEqual(response.status_code, 404)
        fetch.assert_not_called()

    def test_download_uses_utf8_bom_security_headers_and_runs_db_in_worker(self):
        self._entry(self.wallet, date.today(), LedgerDirection.CREDIT, "1", "تحويل تجريبي")
        from app.api.routes import wallet as routes
        original_export = WalletStatementService.export_csv
        original_auth = app.dependency_overrides[get_current_user]
        loop_threads = []

        async def auth():
            loop_threads.append(threading.get_ident())
            return original_auth()

        def export(service, *args, **kwargs):
            self.assertNotEqual(threading.get_ident(), loop_threads[0])
            return original_export(service, *args, **kwargs)

        app.dependency_overrides[get_current_user] = auth
        with patch.object(routes.WalletStatementService, "export_csv", export):
            response = self.client.get(f"/wallets/{self.wallet.id}/statement")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self._csv_rows(response)[0]["transaction_reference"], "تحويل تجريبي")


if __name__ == "__main__":
    unittest.main()
