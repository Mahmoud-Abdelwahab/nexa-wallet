from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import WalletStatus
from app.integrations.moyasar_client import MoyasarClient, MoyasarInvalidResponseError
from app.models.topup import TopUp, TopUpStatus
from app.models.wallet import Wallet
from app.models.transaction import Transaction, TransactionStatus
from app.models.ledger_entry import (
    LedgerEntry,
    LedgerDirection,
    LedgerEntryType,
)
from app.models.moyasar_webhook_event import (
    MoyasarWebhookEventRecord,
)


# Use UTC consistently when checking when an event can be retried.
def utc_now():
    return datetime.now(timezone.utc)


class TopUpPaymentProcessor:

    def __init__(self, db: Session):
        # The worker supplies the DB session; the client sends payment lookups to Moyasar.
        self.db = db
        self.moyasar = MoyasarClient()

    # Temporary problem -> increase retry count -> schedule another attempt -> worker commits.
    # Schedule the retry without making the worker sleep until that time.
    def _retry(self, event, reason: str):
        event.attempts += 1

        # Retry delays in minutes: 2, 4, 8, 16, 32, then 60 for each later retry.
        # The delay is capped; the current code does not cap the number of retries.
        delay = min(2 ** min(event.attempts, 6), 60)

        event.next_attempt_at = utc_now() + timedelta(
            minutes=delay
        )
        event.last_error = reason[:255]

    # Review means no credit: keep the reason, finish this event, and flag the TopUp.
    # A completed TopUp stays completed; a later bad event must not undo its credit.
    def _mark_for_review(
        self,
        event: MoyasarWebhookEventRecord,
        topup: TopUp | None,
        reason: str,
    ) -> None:
        if topup is not None and topup.status != TopUpStatus.COMPLETED:
            topup.status = TopUpStatus.REVIEW_REQUIRED
        event.last_error = reason[:255]
        event.processed_at = utc_now()

    # Business — Steps 4 and 5: verify payment and credit the wallet once.
    # Select a due event and lock its row
    #     ├── No event -> return False
    #     └── Event found -> fetch payment from Moyasar
    #                       ├── HTTP error or invalid response -> schedule retry
    #                       └── Valid response -> lock expected TopUp using event.payment_id
    #                                              ↓
    #                       Check TopUp state, payment status, amount, and currency
    #                           ├── Payment pending -> schedule retry
    #                           ├── ID/amount/currency mismatch -> review required, no credit
    #                           └── Valid -> lock wallet and check owner/currency/ACTIVE
    #                                          ↓
    #                           Create transaction + credit entry + increase balance
    #                                          ↓
    #                           Complete TopUp and mark event processed
    #                                          ↓
    #                           Return True; the worker commits
    # True means an event was handled; it does not guarantee successful funding.
    def process_next(self) -> bool:
        # 1. Select one oldest unfinished event whose next attempt is due.
        event = self.db.scalar(
            select(MoyasarWebhookEventRecord)
            .where(
                MoyasarWebhookEventRecord.processed_at.is_(None),
                MoyasarWebhookEventRecord.next_attempt_at <= utc_now(),
            )
            .order_by(MoyasarWebhookEventRecord.created_at)
            .limit(1)
            # Lock the event; skip rows locked by other workers so each can pick different work.
            .with_for_update(skip_locked=True)
        )

        if event is None:
            return False

        # 2. Fetch payment from Moyasar; a mobile callback alone cannot authorize credit.
        # The network request runs while the event lock is held until the transaction ends.
        try:
            payment = self.moyasar.get_payment(event.payment_id)
        except httpx.HTTPError:
            self._retry(event, "Moyasar API unavailable")
            return True
        except MoyasarInvalidResponseError:
            # Move a malformed response out of the due queue so later events can run.
            self._retry(event, "Invalid Moyasar payment response")
            return True

        # 3. Lock TopUp so different events for the same payment cannot credit it twice.
        # A second worker waits, then sees COMPLETED after the first commits and skips credit.
        # Look up the EXPECTED ID, never an unexpected ID returned by the external API.
        topup = self.db.scalar(
            select(TopUp)
            .where(TopUp.moyasar_payment_id == event.payment_id)
            .with_for_update()
        )

        if payment.id != event.payment_id:
            self._mark_for_review(event, topup, "Payment ID mismatch")
            return True

        if topup is None:
            # No matching funding request: record the reason and finish the event without credit.
            event.last_error = "Unknown payment / no matching TopUp"
            event.processed_at = utc_now()
            return True

        if topup.status in (
            TopUpStatus.COMPLETED,
            TopUpStatus.REVIEW_REQUIRED,
        ):
            # Already completed or awaiting review: finish this event without posting again.
            event.processed_at = utc_now()
            return True

        if payment.status in ("initiated", "authorized"):
            # Payment is not paid yet; schedule another attempt without crediting the wallet.
            self._retry(event, "Payment not yet paid")
            return True

        if payment.status != "paid":
            # Other payment statuses require review; no wallet credit is posted here.
            self._mark_for_review(event, topup, f"Payment status: {payment.status}")
            return True

        expected_currency = topup.currency.value

        # 4. Compare amount in minor units and currency with the stored funding request.
        if (
            payment.amount != topup.amount_minor
            or payment.currency.upper() != expected_currency
        ):
            self._mark_for_review(event, topup, "Amount or currency mismatch")
            return True

        # 5. Lock the wallet row to serialize concurrent balance changes.
        wallet = self.db.scalar(
            select(Wallet)
            .where(Wallet.id == topup.wallet_id)
            .with_for_update()
        )

        # Recheck under the lock: the wallet may have been suspended since POST /topups.
        if (
            wallet is None
            or wallet.user_id != topup.user_id
            or wallet.currency != topup.currency
        ):
            self._mark_for_review(event, topup, "Invalid wallet")
            return True

        if wallet.status != WalletStatus.ACTIVE:
            self._mark_for_review(event, topup, "Wallet is not active")
            return True

        # 6. Convert minor units with Decimal: for example, 10000 fils becomes 100 AED.
        amount = Decimal(topup.amount_minor) / Decimal("100")

        # External funding has no sender wallet inside Nexa.
        transaction = Transaction(
            reference=uuid4().hex,
            sender_wallet_id=None,
            receiver_wallet_id=wallet.id,
            amount=amount,
            currency=topup.currency,
            status=TransactionStatus.SUCCESS,
        )

        self.db.add(transaction)
        # Flush the INSERT to obtain transaction.id; this does not commit the transaction.
        self.db.flush()

        # TOP_UP describes the operation; CREDIT describes the increase in wallet balance.
        ledger_entry = LedgerEntry(
            transaction_id=transaction.id,
            wallet_id=wallet.id,
            direction=LedgerDirection.CREDIT,
            entry_type=LedgerEntryType.TOP_UP,
            amount=amount,
            currency=topup.currency,
        )

        self.db.add(ledger_entry)

        # The wallet row lock protects this change inside the same DB transaction.
        wallet.balance += amount

        # 7. Link the financial transaction and mark the TopUp completed and event processed.
        topup.transaction_id = transaction.id
        topup.status = TopUpStatus.COMPLETED

        event.processed_at = utc_now()

        return True

    # The worker owns commit/rollback when its SessionLocal.begin() block exits.
    # Normal exit commits all changes; an escaping exception rolls them back and releases locks.
