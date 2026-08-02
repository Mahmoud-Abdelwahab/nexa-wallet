from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import Currency, TransactionStatus
from app.database import Base

if TYPE_CHECKING:
    from app.models.ledger_entry import LedgerEntry
    from app.models.wallet import Wallet


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)

    reference: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
    )

    sender_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id"),
        nullable=False,
    )

    receiver_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id"),
        nullable=False,
    )

    amount: Mapped[Decimal] = mapped_column(
        Numeric(19, 4),
        nullable=False,
    )

    currency: Mapped[Currency] = mapped_column(
    SQLEnum(
        Currency,
        name="currency",
    ),
    nullable=False,
)

    status: Mapped[TransactionStatus] = mapped_column(
        SQLEnum(TransactionStatus),
        nullable=False,
        default=TransactionStatus.PENDING,
    )

    gateway_message: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        default=datetime.now,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )

    sender_wallet: Mapped["Wallet"] = relationship(
        foreign_keys=[sender_wallet_id],
        back_populates="sent_transactions",
    )

    receiver_wallet: Mapped["Wallet"] = relationship(
        foreign_keys=[receiver_wallet_id],
        back_populates="received_transactions",
    )

    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction",
    )