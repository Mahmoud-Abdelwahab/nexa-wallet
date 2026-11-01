"""Exercise posting and rollback in SQLite; PostgreSQL locking needs live tests."""

import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.api.routes.transfer import create_transfer
from app.core.enums import Currency, TransactionStatus, WalletStatus
from app.database import Base
from app.main import app
from app.models import LedgerEntry, Transaction, User, Wallet
from app.models.idempotency_record import IdempotencyRecord, IdempotencyStatus
from app.models.ledger_entry import LedgerDirection
from app.repositories.idempotency_repository import IdempotencyRepository
from app.schemas.transfer import TransferRequest
from app.services.transfer_service import (
    IdempotencyConflictError,
    TransferService,
    calculate_request_hash,
)


class SQLiteIdempotencyRepository(IdempotencyRepository):
    # Adapt only the dialect-specific reservation SQL for this isolated fixture.
    def try_reserve(self, user_id, key, request_hash):
        stmt = (
            insert(IdempotencyRecord)
            .values(user_id=user_id, idempotency_key=key,
                    request_hash=request_hash, status=IdempotencyStatus.PROCESSING)
            .on_conflict_do_nothing(index_elements=["user_id", "idempotency_key"])
            .returning(IdempotencyRecord.id)
        )
        return self.db.scalar(stmt) is not None


class TransferIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        users = [User(username=f"user{i}", email=f"user{i}@example.com",
                      mobile=str(i), password_hash="unused",
                      date_of_birth=date(2000, 1, 1)) for i in range(2)]
        self.db.add_all(users)
        self.db.flush()
        wallets = [Wallet(user_id=u.id, currency=Currency.AED, balance=Decimal(b))
                   for u, b in zip(users, ("500", "100"))]
        self.db.add_all(wallets)
        self.db.commit()
        self.uid, self.other_uid = [u.id for u in users]
        self.sid, self.rid = [w.id for w in wallets]
        self.service = TransferService(self.db)
        self.service.idempotency_repository = SQLiteIdempotencyRepository(self.db)

    def transfer(self, amount="150", key="request-1", **kwargs):
        return self.service.transfer(
            user_id=self.uid, sender_wallet_id=self.sid,
            receiver_wallet_id=self.rid, amount=Decimal(amount),
            idempotency_key=key, **kwargs,
        )

    def snapshot(self):
        return (
            self.db.get(Wallet, self.sid).balance,
            self.db.get(Wallet, self.rid).balance,
            *(self.db.scalar(select(func.count()).select_from(model))
              for model in (Transaction, LedgerEntry, IdempotencyRecord)),
        )

    def test_unavailable_wallets_hide_details_and_do_not_post(self):
        for case in ("missing", "inactive", "currency", "another_sender"):
            with self.subTest(case=case):
                receiver = self.db.get(Wallet, self.rid)
                receiver.status = WalletStatus.INACTIVE if case == "inactive" else WalletStatus.ACTIVE
                receiver.currency = Currency.USD if case == "currency" else Currency.AED
                self.db.commit()
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "^Wallet unavailable for this transfer$"):
                    self.service.transfer(
                        user_id=self.other_uid if case == "another_sender" else self.uid,
                        sender_wallet_id=self.sid,
                        receiver_wallet_id=9999 if case == "missing" else self.rid,
                        amount=Decimal("150"), idempotency_key="privacy-test",
                    )
                self.assertEqual(self.snapshot(), before)

    def test_success_and_equivalent_replay_post_only_once(self):
        first = self.transfer()
        self.assertEqual(first.status, TransactionStatus.SUCCESS)
        self.assertEqual(self.snapshot(), (Decimal("350"), Decimal("250"), 1, 2, 1))
        second = self.transfer(amount="150.0000")
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.snapshot(), (Decimal("350"), Decimal("250"), 1, 2, 1))
        record = self.service.idempotency_repository.get_by_key(self.uid, "request-1")
        self.assertEqual(record.status, IdempotencyStatus.COMPLETED)
        self.assertEqual(record.transaction_id, first.id)
        entries = list(self.db.scalars(select(LedgerEntry)))
        self.assertEqual({e.direction for e in entries},
                         {LedgerDirection.DEBIT, LedgerDirection.CREDIT})
        self.assertTrue(all(e.transaction_id == first.id and e.amount == 150
                            and e.currency == Currency.AED for e in entries))

    def test_key_conflict_does_not_change_existing_result(self):
        self.transfer()
        before = self.snapshot()
        for args in ({"amount": "151"}, {"message": "different"}):
            with self.subTest(args=args), self.assertRaises(IdempotencyConflictError):
                self.transfer(**args)
            self.assertEqual(self.snapshot(), before)

    def test_invalid_amounts_do_not_reserve_keys(self):
        for amount in ("0", "-1", "NaN", "sNaN", "Infinity", "-Infinity",
                       "0.00001", "150.00001", "1e100"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.transfer(amount=amount)
            self.assertEqual(self.snapshot(), (Decimal("500"), Decimal("100"), 0, 0, 0))

    def test_failed_validation_releases_reserved_key(self):
        with self.assertRaisesRegex(ValueError, "Insufficient balance"):
            self.transfer(amount="1000")
        self.assertEqual(self.snapshot(), (Decimal("500"), Decimal("100"), 0, 0, 0))
        self.transfer()

    def test_posting_failure_rolls_back_key_entries_and_balances(self):
        def fail_commit():
            self.db.flush()
            raise RuntimeError("Simulated failure after flush")
        with patch.object(self.db, "commit", side_effect=fail_commit):
            with self.assertRaisesRegex(RuntimeError, "Simulated failure"):
                self.transfer()
        self.assertEqual(self.snapshot(), (Decimal("500"), Decimal("100"), 0, 0, 0))
        self.transfer()

    def test_missing_reserved_record_rolls_back_posting(self):
        with patch.object(
            self.service.idempotency_repository, "get_by_key", return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, "Idempotency record missing"):
                self.transfer()
        self.assertEqual(
            self.snapshot(), (Decimal("500"), Decimal("100"), 0, 0, 0)
        )
        # The rolled-back reservation does not prevent a successful retry.
        self.transfer()

    def test_same_key_is_scoped_to_user(self):
        first = self.transfer()
        second = self.service.transfer(
            user_id=self.other_uid, sender_wallet_id=self.rid,
            receiver_wallet_id=self.sid, amount=Decimal("10"),
            idempotency_key="request-1",
        )
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.snapshot(), (Decimal("360"), Decimal("240"), 2, 4, 2))

    def test_uncompleted_result_is_not_reposted(self):
        self.service.idempotency_repository.try_reserve(
            self.uid, "request-1",
            calculate_request_hash(self.sid, self.rid, Decimal("150"), None),
        )
        self.db.commit()
        with self.assertRaisesRegex(RuntimeError, "Transfer result unavailable"):
            self.transfer()
        self.assertEqual(self.snapshot(), (Decimal("500"), Decimal("100"), 0, 0, 1))

    def test_api_requires_header_and_maps_conflict_to_409(self):
        parameters = app.openapi()["paths"]["/transfers"]["post"]["parameters"]
        header = next(p for p in parameters if p["name"] == "Idempotency-Key")
        self.assertTrue(header["required"])
        self.assertEqual(header["schema"]["maxLength"], 128)
        with patch("app.api.routes.transfer.TransferService") as service:
            service.return_value.transfer.side_effect = IdempotencyConflictError("Conflict")
            with self.assertRaises(HTTPException) as error:
                create_transfer(
                    TransferRequest(sender_wallet_id=self.sid, receiver_wallet_id=self.rid, amount=150),
                    idempotency_key="request-1", current_user=self.db.get(User, self.uid), db=self.db,
                )
        self.assertEqual(error.exception.status_code, 409)
