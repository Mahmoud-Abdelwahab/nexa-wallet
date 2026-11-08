from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IdempotencyStatus(str, Enum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
     # this is the most important constraint to ensure that a user cannot have two records with the same idempotency key
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_idempotency_user_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    request_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
# transactions.id we link the idempotency record to the transaction that was created as a result of the request. This is important for ensuring that if a request is retried, we can check if a transaction has already been created for that request and avoid creating a duplicate transaction.
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"),
        nullable=True,
    )
   # why status is important: The status field is crucial for tracking the state of the idempotency record. It indicates whether the request is still being processed or has been completed. This helps in managing retries and ensuring that duplicate processing does not occur for the same request.
   # PROCESSING / COMPLETED: بنفرق بين طلب لسه بيتنفذ وطلب اتنفذ واتحفظت نتيجته.
    status: Mapped[IdempotencyStatus] = mapped_column(
        SQLEnum(IdempotencyStatus),
        default=IdempotencyStatus.PROCESSING,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )