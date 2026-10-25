from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.enums import TransactionStatus, WalletStatus
from app.models.ledger_entry import LedgerDirection, LedgerEntry, LedgerEntryType
from app.models.transaction import Transaction
from app.repositories.wallet_repository import WalletRepository
from app.repositories.transaction_repository import TransactionRepository
from app.repositories.ledger_repository import LedgerRepository


class TransferService:

    def __init__(self, db: Session):
        self.db = db

        self.wallet_repository = WalletRepository(db)
        self.transaction_repository = TransactionRepository(db)
        self.ledger_repository = LedgerRepository(db)

    # TransferService.transfer()
    #          |
    #          v
    # Validate amount > 0 and distinct wallet ids
    #          |
    #          v
    # Lock both wallets in ascending id order
    #          |
    #          v
    # Validate both wallets exist
    #          |
    #          v
    # Validate sender ownership
    #          |
    #          v
    # Validate both wallets are ACTIVE
    #          |
    #          v
    # Validate same currency
    #          |
    #          v
    # Validate sufficient sender balance
    #          |
    #          v
    # Create Transaction(PENDING); flush for id
    #          |
    #          v
    # Create sender DEBIT + receiver CREDIT entries
    # Both entries use LedgerEntryType.TRANSFER;
    # LedgerDirection determines DEBIT or CREDIT.
    #          |
    #          v
    # Update both wallet balances
    #          |
    #          v
    # Mark Transaction(SUCCESS)
    #          |
    #          v
    # COMMIT everything once
    #
    # Any exception in the locking/posting try block
    #          |
    #          v
    # ROLLBACK all uncommitted changes; re-raise
    #
    # Amount/same-wallet failures exit before locking or posting.
    def transfer(
        self,
        user_id: int,
        sender_wallet_id: int,
        receiver_wallet_id: int,
        amount: Decimal,
        message: str | None = None,
    ) -> Transaction:
        if amount <= 0:
            raise ValueError("Transfer amount must be greater than zero")

        if sender_wallet_id == receiver_wallet_id:
            raise ValueError(
                "Sender and receiver wallets cannot be the same"
            )

        try:
            wallets = self.wallet_repository.get_wallets_for_update(
                [sender_wallet_id, receiver_wallet_id]
            )

            if len(wallets) != 2:
                raise ValueError("Wallet not found")

            wallets_by_id = {
                wallet.id: wallet
                for wallet in wallets
            }

            sender_wallet = wallets_by_id[sender_wallet_id]
            receiver_wallet = wallets_by_id[receiver_wallet_id]

            if sender_wallet.user_id != user_id:
                raise ValueError("Wallet not found")

            if sender_wallet.status != WalletStatus.ACTIVE:
                raise ValueError("Sender wallet is not active")

            if receiver_wallet.status != WalletStatus.ACTIVE:
                raise ValueError("Receiver wallet is not active")

            if sender_wallet.currency != receiver_wallet.currency:
                raise ValueError(
                    "Wallet currencies must match"
                )

            if sender_wallet.balance < amount:
                raise ValueError("Insufficient balance")

            transaction = Transaction(
                reference=f"TXN-{uuid4().hex}",
                sender_wallet_id=sender_wallet.id,
                receiver_wallet_id=receiver_wallet.id,
                amount=amount,
                currency=sender_wallet.currency,
                status=TransactionStatus.PENDING,
                gateway_message=message,
            )

            self.transaction_repository.create(transaction)

            debit_entry = LedgerEntry(
                transaction_id=transaction.id,
                wallet_id=sender_wallet.id,
                direction=LedgerDirection.DEBIT,
                entry_type=LedgerEntryType.TRANSFER,
                amount=amount,
                currency=sender_wallet.currency,
            )

            credit_entry = LedgerEntry(
                transaction_id=transaction.id,
                wallet_id=receiver_wallet.id,
                direction=LedgerDirection.CREDIT,
                entry_type=LedgerEntryType.TRANSFER,
                amount=amount,
                currency=receiver_wallet.currency,
            )

            self.ledger_repository.create(debit_entry)
            self.ledger_repository.create(credit_entry)

            sender_wallet.balance -= amount
            receiver_wallet.balance += amount

            transaction.status = TransactionStatus.SUCCESS

            self.db.commit()

            return transaction

        except Exception:
            self.db.rollback()
            raise
