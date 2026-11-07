from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# once the webhook event is received from moyasar, we store it in the database for processing by a worker
# then we will call the get_payment endpoint to verify the payment status and amount before crediting the wallet
# once the payment is verified, we will mark the webhook event as processed and update the wallet balance in single transaction
class MoyasarWebhookEventRecord(Base):
    __tablename__ = "moyasar_webhook_events"

    id: Mapped[str] = mapped_column(
        String(100), primary_key=True
    )

    payment_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )

    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )

    attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    last_error: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )