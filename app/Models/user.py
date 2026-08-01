from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.wallet import Wallet


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)

    username: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    mobile: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
    )

    date_of_birth: Mapped[date] = mapped_column(
        nullable=False,
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

    wallets: Mapped[list["Wallet"]] = relationship(
        back_populates="user",
    )