from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.core.enums import Currency


class TopUpStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TopUp(Base):
    """Track a wallet funding request through Moyasar, including pending/failed attempts.

    topups = the funding request; transactions = the Nexa financial operation;
    ledger_entries = the operation's effect on wallet balances.
    Creating a TopUp alone does not credit the wallet. Once posted successfully,
    transaction_id links this request to its financial transaction.
    Internal wallet-to-wallet transfers do not need a TopUp record.
    """

    __tablename__ = "topups"

    __table_args__ = (
        CheckConstraint(
            "amount_minor > 0",
            name="ck_topup_amount_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id"),
        nullable=False,
    )
    # amount_minor is the amount in the smallest currency unit (e.g., cents for USD, fils for AED). This is important for ensuring precision in financial transactions and avoiding issues with floating-point arithmetic.
    amount_minor: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    currency: Mapped[Currency] = mapped_column(
        SQLEnum(Currency, name="currency"),
        nullable=False,
    )

    moyasar_payment_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        nullable=False,
        default=lambda: str(uuid4()),
    )

    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"),
        unique=True,
        nullable=True,
    )

    status: Mapped[TopUpStatus] = mapped_column(
        SQLEnum(TopUpStatus, name="topup_status"),
        default=TopUpStatus.PENDING,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
