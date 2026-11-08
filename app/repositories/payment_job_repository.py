from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.moyasar_webhook_event import MoyasarWebhookEventRecord


@dataclass(frozen=True)
class ClaimedPaymentJob:
    # Plain values survive session closure; attempt identifies this claim's owner.
    event_id: str
    payment_id: str
    attempt: int


class PaymentJobRepository:
    def __init__(self, db: Session):
        self.db = db

    # Select one due unfinished event -> short FOR UPDATE SKIP LOCKED
    #     ↓
    # Increment attempt -> next_attempt_at = now + 60 seconds -> return plain job
    #     ↓
    # Worker commits and closes session BEFORE calling Moyasar
    #
    # Lease example: claimed at 15:10 -> unavailable until 15:11.
    # A crash leaves it eligible again after 15:11; no permanent lock is held.
    def claim_next(self) -> ClaimedPaymentJob | None:
        now = datetime.now(timezone.utc)
        event = self.db.scalar(select(MoyasarWebhookEventRecord).where(
            MoyasarWebhookEventRecord.processed_at.is_(None),
            MoyasarWebhookEventRecord.next_attempt_at <= now,
        ).order_by(MoyasarWebhookEventRecord.created_at, MoyasarWebhookEventRecord.id)
          .limit(1).with_for_update(skip_locked=True))
        if event is None:
            return None
        event.attempts += 1
        event.next_attempt_at = now + timedelta(seconds=60)
        return ClaimedPaymentJob(event.id, event.payment_id, event.attempts)
