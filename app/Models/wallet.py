from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import Currency, WalletStatus
from app.database import Base

if TYPE_CHECKING:
    from app.models.ledger_entry import LedgerEntry
    from app.models.transaction import Transaction
    from app.models.user import User


class Wallet(Base):
    __tablename__ = "wallets"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "currency",
            name="uq_wallet_user_currency",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    currency: Mapped[Currency] = mapped_column(
        SQLEnum(Currency),
        nullable=False,
    )

    balance: Mapped[Decimal] = mapped_column(
        Numeric(19, 4),
        nullable=False,
        default=Decimal("0"),
    )

    status: Mapped[WalletStatus] = mapped_column(
        SQLEnum(WalletStatus),
        nullable=False,
        default=WalletStatus.ACTIVE,
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

    user: Mapped["User"] = relationship(
        back_populates="wallets",
    )

    sent_transactions: Mapped[list["Transaction"]] = relationship(
        foreign_keys="Transaction.sender_wallet_id",
        back_populates="sender_wallet",
    )

    received_transactions: Mapped[list["Transaction"]] = relationship(
        foreign_keys="Transaction.receiver_wallet_id",
        back_populates="receiver_wallet",
    )

    # 1 wallet can have many ledger entries
    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(
    back_populates="wallet",
    )