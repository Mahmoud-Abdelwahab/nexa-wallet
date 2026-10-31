import hashlib
import json
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.core.enums import TransactionStatus, WalletStatus
from app.models.idempotency_record import IdempotencyStatus
from app.models.ledger_entry import LedgerDirection, LedgerEntry, LedgerEntryType
from app.models.transaction import Transaction
from app.repositories.idempotency_repository import IdempotencyRepository
from app.repositories.ledger_repository import LedgerRepository
from app.repositories.transaction_repository import TransactionRepository
from app.repositories.wallet_repository import WalletRepository

# Request fingerprint flow:
# sender id + receiver id + normalized amount + message
#                         |
#                         v
# Fixed amount format + sorted JSON keys + no extra spaces
#                         |
#                         v
# UTF-8 bytes -> SHA-256 -> 64-character request_hash
#                         |
#                         v
# Compare with the hash stored for this user's Idempotency-Key.
#
# The key identifies a retry; the hash checks that its data has not changed.
# Same user + same key + same data -> return the completed saved transaction.
# Same user + same key + changed data -> reject with 409 Conflict.
# Different key + same data -> a new request, not a replay.
def calculate_request_hash(
    sender_wallet_id: int,
    receiver_wallet_id: int,
    amount: Decimal,
    message: str | None,
) -> str:
    # Equal amounts such as 150 and 150.0 both become "150.0000".
    payload = {
        "sender_wallet_id": sender_wallet_id,
        "receiver_wallet_id": receiver_wallet_id,
        "amount": format(amount, ".4f"),
        "message": message,
    }
    # sort_keys=True sorts JSON field names, not their values.
    # Example: {"b": 2, "a": 1} and {"a": 1, "b": 2} serialize identically.
    # Field order must not make the same request look like different data.
    # separators removes formatting spaces for a stable JSON representation.
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    # Hash the request data so we can detect changed input on a key's retry.
    # This is a fingerprint, not encryption; the key is stored separately.
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class IdempotencyConflictError(Exception):
    """The same user's key was reused with different request data."""


class TransferService:
    def __init__(self, db: Session):
        self.db = db
        self.wallet_repository = WalletRepository(db)
        self.transaction_repository = TransactionRepository(db)
        self.ledger_repository = LedgerRepository(db)
        self.idempotency_repository = IdempotencyRepository(db)

    # Three steps to read this service:
    # transfer()              -> validate the request and manage commit/rollback
    # _get_existing_transfer() -> check a retry and return its saved transaction
    # _create_new_transfer()   -> check wallets and record a new money movement
    #
    # TransferService.transfer()
    #          |
    #          v
    # Validate finite amount > 0, max 4 decimal places, different wallets
    #          |
    #          v
    # Normalize amount -> calculate_request_hash(...)
    #          |
    #          v
    # Reserve (user_id, idempotency_key) with request_hash
    #          |
    #          +-- Existing key -> _get_existing_transfer()
    #          |       |
    #          |       v
    #          |   Load the saved idempotency record
    #          |       |
    #          |       +-- Missing record -> error
    #          |       |
    #          |       v
    #          |   Compare saved hash with incoming request_hash
    #          |       |
    #          |       +-- Different -> IdempotencyConflictError -> HTTP 409
    #          |       |
    #          |       v
    #          |   Same hash: COMPLETED and transaction_id available?
    #          |       |
    #          |       +-- No -> result unavailable error
    #          |       |
    #          |       v
    #          |   Load the original Transaction
    #          |       |
    #          |       +-- Missing transaction -> error
    #          |       |
    #          |       v
    #          |   COMMIT read/replay boundary -> return original Transaction
    #          |   No wallet updates and no new ledger entries
    #          |
    #          +-- New key -> _create_new_transfer() (not committed yet)
    #                  |
    #                  v
    #              Lock both wallets in ascending id order
    #                  |
    #                  v
    #              Check both wallets exist; identify them by id
    #                  |
    #                  v
    #              Validate sender ownership
    #                  |
    #                  v
    #              Validate sender and receiver are ACTIVE
    #                  |
    #                  v
    #              Validate same currency
    #                  |
    #                  v
    #              Validate sufficient sender balance
    #                  |
    #                  v
    #              Create Transaction(PENDING); flush for its id
    #                  |
    #                  v
    #              Create sender DEBIT + receiver CREDIT ledger entries
    #              Both use LedgerEntryType.TRANSFER
    #                  |
    #                  v
    #              Subtract sender balance; add receiver balance
    #                  |
    #                  v
    #              Mark Transaction = SUCCESS
    #                  |
    #                  v
    #              Load reserved record; missing -> error and rollback
    #                  |
    #                  v
    #              Link idempotency record to Transaction; mark COMPLETED
    #                  |
    #                  v
    #              ONE COMMIT: key + transaction + entries + balances
    #                  |
    #                  v
    #              Return the new Transaction
    #
    # Any exception inside the database try block
    #          |
    #          v
    # ROLLBACK all uncommitted changes; release locks; re-raise
    #
    # Initial amount/same-wallet errors exit before reserving or posting.
    # The unique user/key constraint coordinates competing reservations.
    # A reservation is never committed separately from its financial result.
    def transfer(
        self,
        user_id: int,
        sender_wallet_id: int,
        receiver_wallet_id: int,
        amount: Decimal,
        idempotency_key: str,
        message: str | None = None,
    ) -> Transaction:
        # 1. Reject invalid amounts before reserving a key or locking wallets.
        if not amount.is_finite() or amount <= 0:
            raise BusinessError("Invalid transfer amount")

        try:
            normalized_amount = amount.quantize(Decimal("0.0001"))
        except InvalidOperation:
            raise BusinessError("Invalid amount precision")

        if amount != normalized_amount:
            raise BusinessError("Maximum 4 decimal places allowed")

        if sender_wallet_id == receiver_wallet_id:
            raise BusinessError("Wallets cannot be the same")

        request_hash = calculate_request_hash(
            sender_wallet_id,
            receiver_wallet_id,
            normalized_amount,
            message,
        )

        try:
            # 2. Reserve this user's key in the same database transaction.
            is_new_request = self.idempotency_repository.try_reserve(
                user_id=user_id,
                key=idempotency_key,
                request_hash=request_hash,
            )

            if is_new_request:
                transaction = self._create_new_transfer(
                    user_id=user_id,
                    sender_wallet_id=sender_wallet_id,
                    receiver_wallet_id=receiver_wallet_id,
                    amount=normalized_amount,
                    idempotency_key=idempotency_key,
                    message=message,
                )
            else:
                transaction = self._get_existing_transfer(
                    user_id=user_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )

            # Only transfer() commits, after either path finishes successfully.
            self.db.commit()
            return transaction
        except Exception:
            self.db.rollback()
            raise

    def _get_existing_transfer(
        self,
        user_id: int,
        idempotency_key: str,
        request_hash: str,
    ) -> Transaction:
        # Read the result saved for this user's key.
        record = self.idempotency_repository.get_by_key(
            user_id=user_id,
            key=idempotency_key,
        )
        if record is None:
            raise RuntimeError("Idempotency record missing")

        # The same key must refer to the same request data.
        if record.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency key used with different data"
            )

        if (
            record.status != IdempotencyStatus.COMPLETED
            or record.transaction_id is None
        ):
            raise RuntimeError("Transfer result unavailable")

        # Return the original transaction without moving money again.
        transaction = self.transaction_repository.get_by_id(record.transaction_id)
        if transaction is None:
            raise RuntimeError("Transaction record missing")
        return transaction

    def _create_new_transfer(
        self,
        user_id: int,
        sender_wallet_id: int,
        receiver_wallet_id: int,
        amount: Decimal,
        idempotency_key: str,
        message: str | None,
    ) -> Transaction:
        # transfer() already validated the amount and reserved the key.
        # 3. Lock both wallets in ascending id order until commit/rollback.
        wallets = self.wallet_repository.get_wallets_for_update(
            [sender_wallet_id, receiver_wallet_id]
        )
        if len(wallets) != 2:
            raise BusinessError("Wallet unavailable for this transfer")

        # Lock order is not sender/receiver order; identify wallets by id.
        wallets_by_id = {wallet.id: wallet for wallet in wallets}
        sender = wallets_by_id[sender_wallet_id]
        receiver = wallets_by_id[receiver_wallet_id]

        # 4. Check ownership first; hide receiver existence, status, and currency.
        if sender.user_id != user_id:
            raise BusinessError("Wallet unavailable for this transfer")
        if sender.status != WalletStatus.ACTIVE:
            raise BusinessError("Sender wallet is not active")
        if receiver.status != WalletStatus.ACTIVE:
            raise BusinessError("Wallet unavailable for this transfer")
        if sender.currency != receiver.currency:
            raise BusinessError("Wallet unavailable for this transfer")
        if sender.balance < amount:
            raise BusinessError("Insufficient balance")

        # 5. Flush the pending transaction to obtain its ledger reference.
        transaction = Transaction(
            reference=uuid4().hex,
            sender_wallet_id=sender.id,
            receiver_wallet_id=receiver.id,
            amount=amount,
            currency=sender.currency,
            status=TransactionStatus.PENDING,
            gateway_message=message,
        )
        self.transaction_repository.create(transaction)

        # 6. Record one debit and one credit for this transfer.
        self.ledger_repository.create(
            LedgerEntry(
                transaction_id=transaction.id,
                wallet_id=sender.id,
                direction=LedgerDirection.DEBIT,
                entry_type=LedgerEntryType.TRANSFER,
                amount=amount,
                currency=sender.currency,
            )
        )
        self.ledger_repository.create(
            LedgerEntry(
                transaction_id=transaction.id,
                wallet_id=receiver.id,
                direction=LedgerDirection.CREDIT,
                entry_type=LedgerEntryType.TRANSFER,
                amount=amount,
                currency=receiver.currency,
            )
        )

        # 7. Apply both balance changes and mark the transfer successful.
        sender.balance -= amount
        receiver.balance += amount
        transaction.status = TransactionStatus.SUCCESS

        # 8. Save the result so a retry can return the same transaction.
        record = self.idempotency_repository.get_by_key(
            user_id=user_id,
            key=idempotency_key,
        )
        # Stop and roll back if the reserved record cannot be found.
        if record is None:
            raise RuntimeError("Idempotency record missing")

        record.transaction_id = transaction.id
        record.status = IdempotencyStatus.COMPLETED

        # Leave commit/rollback to transfer(), so all changes stay together.
        return transaction
