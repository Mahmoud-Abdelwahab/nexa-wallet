from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import WalletStatus
from app.integrations.moyasar_client import MoyasarPayment
from app.models.topup import TopUp, TopUpStatus
from app.models.wallet import Wallet
from app.models.transaction import Transaction, TransactionStatus
from app.models.ledger_entry import LedgerEntry, LedgerDirection, LedgerEntryType
from app.models.moyasar_webhook_event import MoyasarWebhookEventRecord
from app.repositories.payment_job_repository import ClaimedPaymentJob

FAST_CHECK_LIMIT = 8


def utc_now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SupportAlert:
    # Safe identifiers/reason only; the durable source is the event row, not the log.
    event_id: str
    payment_id: str
    reason: str


class TopUpPaymentProcessor:
    def __init__(self, db: Session):
        # Finalization uses DB only; the worker has already fetched the payment.
        self.db = db

    # Lock event -> compare claim attempt -> ignore finished or superseded work.
    # Example: A owns attempt 1; B reclaims as attempt 2. A cannot overwrite B.
    def _get_claimed_event(self, job: ClaimedPaymentJob) -> MoyasarWebhookEventRecord | None:
        event = self.db.scalar(select(MoyasarWebhookEventRecord).where(
            MoyasarWebhookEventRecord.id == job.event_id,
        ).with_for_update())
        if (event is None or event.processed_at is not None
                or event.attempts != job.attempt or event.payment_id != job.payment_id):
            return None
        return event

    def _get_topup(self, payment_id: str) -> TopUp | None:
        # Lock the expected TopUp, never a different ID returned by the provider.
        return self.db.scalar(select(TopUp).where(
            TopUp.moyasar_payment_id == payment_id,
        ).with_for_update())

    def _alert(self, event: MoyasarWebhookEventRecord, reason: str) -> SupportAlert:
        # Persist with the outcome; the worker logs only AFTER the commit succeeds.
        safe_reason = reason[:255]
        event.last_error = safe_reason
        event.support_alert_at = utc_now()
        event.processed_at = utc_now()
        return SupportAlert(event.id, event.payment_id, safe_reason)

    def _mark_for_review(
        self, event: MoyasarWebhookEventRecord, topup: TopUp | None, reason: str,
    ) -> SupportAlert:
        if topup is not None and topup.status == TopUpStatus.PENDING:
            topup.status = TopUpStatus.REVIEW_REQUIRED
        return self._alert(event, reason)

    # Lock claim -> lock TopUp -> terminal? Finish event only
    #     |
    #     +-- 404 / initiated / authorized (waiting_for_payment=True)
    #     |       Younger than 15 minutes AND claim count < 8 -> check in 2 minutes
    #     |       Otherwise -> check again in 60 minutes
    #     |       Keep PENDING; age/count alone must not block a late payment
    #     |
    #     +-- Technical error -> backoff, then hourly checks + one support alert
    #         Keep PENDING: an outage is not proof of a failed payment
    #
    # Example: 404 at 15:05 -> PENDING; paid webhook at 15:30 -> credit once.
    # The same event stays unfinished for later checks if the webhook never arrives.
    # Never reset attempts: it also protects against a stale worker's result.
    def finish_retry(
        self, job: ClaimedPaymentJob, reason: str, *, waiting_for_payment: bool = False,
    ) -> SupportAlert | None:
        event = self._get_claimed_event(job)
        if event is None:
            return None
        topup = self._get_topup(job.payment_id)
        if topup is not None and topup.status != TopUpStatus.PENDING:
            event.processed_at = utc_now()
            return None
        if waiting_for_payment and topup is not None:
            # This is a waiting policy, not a failed-payment decision.
            now = utc_now()
            created_at = topup.created_at
            if created_at.tzinfo is None:  # SQLite tests return naive UTC timestamps.
                created_at = created_at.replace(tzinfo=timezone.utc)
            fast_check = now - created_at < timedelta(minutes=15) and event.attempts < FAST_CHECK_LIMIT
            delay = 2 if fast_check else 60
            event.last_error = reason[:255]
            event.next_attempt_at = now + timedelta(minutes=delay)
            return None
        # Technical failures slow down; they do not make a pending payment terminal.
        event.last_error = reason[:255]
        delay = min(2 ** min(event.attempts, 6), 60)
        event.next_attempt_at = utc_now() + timedelta(minutes=delay)
        if event.attempts >= FAST_CHECK_LIMIT and event.support_alert_at is None:
            # Alert once but leave processed_at NULL so recovery can continue.
            event.support_alert_at = utc_now()
            return SupportAlert(event.id, event.payment_id, f"Slow retries: {reason}"[:255])
        return None

    # Verified API response (no HTTP call inside this function)
    #     ↓
    # Lock event -> stale/completed claim? Ignore
    #     ↓
    # Lock expected TopUp -> missing? Retry -> terminal? Finish event without credit
    #     ↓
    # Compare ID + amount + currency -> mismatch? REVIEW_REQUIRED + support alert
    #     ↓
    # Payment status:
    #     ├── initiated/authorized -> keep PENDING; check less often as it ages
    #     ├── failed -> TopUp FAILED + support alert; no credit, no automatic retry
    #     ├── other -> REVIEW_REQUIRED + support alert
    #     └── paid -> lock wallet -> check owner/currency/ACTIVE
    #                       ↓
    #              Transaction + CREDIT ledger + balance + COMPLETED + event finished
    #                       ↓
    #              Caller COMMITs all together; errors roll everything back
    # Example: paid 50 AED -> exactly one 50 AED credit even if two workers arrive.
    def finish_payment(self, job: ClaimedPaymentJob, payment: MoyasarPayment) -> SupportAlert | None:
        event = self._get_claimed_event(job)
        if event is None:
            return None
        topup = self._get_topup(job.payment_id)
        if topup is None:
            return self.finish_retry(job, "TopUp not found")
        # FAILED and REVIEW_REQUIRED are not automatically reopened by another event.
        if topup.status != TopUpStatus.PENDING:
            event.processed_at = utc_now()
            return None
        if payment.id != job.payment_id:
            return self._mark_for_review(event, topup, "Payment ID mismatch")
        if payment.amount != topup.amount_minor or payment.currency.upper() != topup.currency.value:
            return self._mark_for_review(event, topup, "Amount or currency mismatch")
        if payment.status in ("initiated", "authorized"):
            return self.finish_retry(job, "Payment not yet paid", waiting_for_payment=True)
        if payment.status == "failed":
            topup.status = TopUpStatus.FAILED
            return self._alert(event, "Moyasar payment failed; no wallet credit posted")
        if payment.status != "paid":
            # Do not copy arbitrary provider messages or card details into alerts.
            return self._mark_for_review(event, topup, "Payment status requires review")
        # Keep the support reason as history if a previously alerted payment recovers.
        if event.support_alert_at is None:
            event.last_error = None

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
            return self._mark_for_review(event, topup, "Invalid wallet")
        if wallet.status != WalletStatus.ACTIVE:
            return self._mark_for_review(event, topup, "Wallet is not active")

        # All financial writes stay in this same finalization transaction.
        amount = Decimal(topup.amount_minor) / Decimal("100")
        transaction = Transaction(
            reference=uuid4().hex,
            sender_wallet_id=None,
            receiver_wallet_id=wallet.id,
            amount=amount,
            currency=topup.currency,
            status=TransactionStatus.SUCCESS,
        )
        self.db.add(transaction)
        self.db.flush()  # Obtain transaction.id without committing.
        self.db.add(LedgerEntry(
            transaction_id=transaction.id,
            wallet_id=wallet.id,
            direction=LedgerDirection.CREDIT,
            entry_type=LedgerEntryType.TOP_UP,
            amount=amount,
            currency=topup.currency,
        ))
        wallet.balance += amount
        topup.transaction_id = transaction.id
        topup.status = TopUpStatus.COMPLETED
        event.processed_at = utc_now()
        return None
