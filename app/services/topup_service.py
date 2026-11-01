from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import WalletStatus
from app.models.topup import TopUp, TopUpStatus
from app.repositories.topup_repository import TopUpRepository
from app.repositories.wallet_repository import WalletRepository

"""
iOS / Postman
    │
    │ POST /topups
    │ wallet_id = 1
    │ amount = 50.00
    ▼
TopUp Router
    │
    ▼
get_current_user()
    │
    ▼
TopUpService
    │
    ├── Validate wallet ownership
    ├── Check wallet is ACTIVE
    ├── Check currency is supported
    ├── Convert 50.00 → 5000 minor units
    └── Generate givenID
    │
    ▼
TopUpRepository
    │
    ▼
PostgreSQL
    │
    └── Save TopUp(PENDING)
    │
    ▼
Return givenID + amount + currency
"""
class TopUpService:

    def __init__(self, db: Session):
        self.db = db
        self.topup_repository = TopUpRepository(db)
        self.wallet_repository = WalletRepository(db)

    def create_topup(
        self,
        user_id: int,
        wallet_id: int,
        amount: Decimal,
    ) -> TopUp:

        wallet = self.wallet_repository.get_by_id_and_user(
            wallet_id=wallet_id,
            user_id=user_id,
        )

        if wallet is None:
            raise LookupError("Wallet not found")

        if wallet.status != WalletStatus.ACTIVE:
            raise ValueError("Wallet is not active")

        allowed_currencies = {
            item.strip().upper()
            for item in settings.MOYASAR_TOPUP_ALLOWED_CURRENCIES.split(",")
            if item.strip()
        }

        if wallet.currency.value not in allowed_currencies:
            raise ValueError("Currency is not enabled for top up")

        # For supported two-decimal currencies (AED/SAR)
        minor_amount = amount * Decimal("100")

        if (
            not amount.is_finite()
            or amount <= 0
            or minor_amount != minor_amount.to_integral_value()
        ):
            raise ValueError("Invalid top up amount")

        amount_minor = int(minor_amount)

        try:
            topup = TopUp(
                user_id=user_id,
                wallet_id=wallet.id,
                amount_minor=amount_minor,
                currency=wallet.currency,
                moyasar_payment_id=str(uuid4()),
                status=TopUpStatus.PENDING,
            )

            self.topup_repository.create(topup)

            self.db.commit()
            self.db.refresh(topup)

            return topup

        except Exception:
            self.db.rollback()
            raise

    def get_topup(
        self,
        topup_id: UUID,
        user_id: int,
    ) -> TopUp:

        topup = self.topup_repository.get_by_id_for_user(
            topup_id=topup_id,
            user_id=user_id,
        )

        if topup is None:
            raise LookupError("Top up not found")

        return topup