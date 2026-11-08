from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Durable event inbox: committed notifications survive a server restart.
# The worker reads an event and fetches payment details before crediting the wallet.
# Successful credit and event completion are committed in one DB transaction.
class MoyasarWebhookEventRecord(Base):
    """Database record for one notification waiting for background processing.

    Source: the webhook route saves selected fields from an authenticated event.
    Use: the worker selects due records and tracks retries or completion here.
    This is the durable inbox, not the payment or financial transaction itself.
    """

    __tablename__ = "moyasar_webhook_events"

    # Moyasar Event ID deduplicates notifications; it is separate from the Payment ID.
    id: Mapped[str] = mapped_column(
        String(100), primary_key=True
    )

    # Payment ID from webhook data["id"]; used by the worker to fetch the payment.
    payment_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )

    # Notification type received from Moyasar, for example payment_paid.
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )

    # Claim counter and ownership version; increases before each processing attempt.
    # Legacy values counted scheduled retries; they are retained as prior attempts.
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    # Last processing problem or review reason; exclude secrets and card details.
    last_error: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # Eligibility time: a 60-second lease during processing, or a later retry deadline.
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # None means unfinished; a timestamp means handled, including review outcomes.
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Durable support flag for failed payments/review; last_error holds a safe reason.
    # Separate from processed_at: finished events may still need human attention.
    support_alert_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Time the notification was first stored.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
