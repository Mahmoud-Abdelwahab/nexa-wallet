from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Enum as SQLEnum,
    ForeignKey,
    Numeric,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.wallet import Wallet
from app.core.enums import Currency
from app.database import Base

if TYPE_CHECKING:
    from app.models.transaction import Transaction


class LedgerDirection(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class LedgerEntryType(str, Enum):
    TRANSFER = "TRANSFER"
    TOP_UP = "TOP_UP"
    WITHDRAW = "WITHDRAW"
    FEE = "FEE"
    TAX = "TAX"
    CASHBACK = "CASHBACK"


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    __table_args__ = (
        CheckConstraint(
            "amount > 0",
            name="check_ledger_entry_amount_positive",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
    )

    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id"),
        nullable=False,
    )

    wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id"),
        nullable=False,
    )

    direction: Mapped[LedgerDirection] = mapped_column(
        SQLEnum(LedgerDirection),
        nullable=False,
    )

    entry_type: Mapped[LedgerEntryType] = mapped_column(
        SQLEnum(LedgerEntryType),
        nullable=False,
    )

    amount: Mapped[Decimal] = mapped_column(
        Numeric(19, 4),
        nullable=False,
    )

    currency: Mapped[Currency] = mapped_column(
        SQLEnum(Currency),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        default=datetime.now,
        nullable=False,
    )

    transaction: Mapped["Transaction"] = relationship(
    back_populates="ledger_entries",
    )

    wallet: Mapped["Wallet"] = relationship(
    back_populates="ledger_entries",
    )
    
    