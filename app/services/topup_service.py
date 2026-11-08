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
    │ Idempotency-Key: one key for this funding request and its retries
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
    ├── Existing user/key? Compare data and return the saved request
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
    └── Save TopUp(PENDING) + key together; unique user/key prevents duplicates
    │
    ▼
Return givenID + amount + currency
"""
class TopUpIdempotencyConflictError(ValueError):
    """A funding key was reused with different request data."""


class TopUpService:

    def __init__(self, db: Session):
        self.db = db
        self.topup_repository = TopUpRepository(db)
        self.wallet_repository = WalletRepository(db)

    # TopUp creation flow (no payment or wallet credit happens here)
    #
    # POST /topups + Idempotency-Key + authenticated user
    #                         |
    #                         v
    #                  create_topup()
    #                         |
    #                         v
    # 1. Validate amount: finite, positive, within limit, max 2 decimal places
    #    Convert to minor units: 3 / 3.0 / 3.00 -> 300
    #    Validate key: not blank, maximum 128 characters
    #                         |
    #                         +-- Invalid -> error before any INSERT
    #                         |
    #                         v
    # 2. get_by_key(user_id, idempotency_key)
    #                         |
    #          +--------------+----------------+
    #          |                               |
    #          v                               v
    #     Key found                       Key not found
    #     Keep saved TopUp                _create_new_topup()
    #          |                               |
    #          |                               v
    #          |                    3. Validate wallet ownership,
    #          |                       ACTIVE status, allowed currency
    #          |                               |
    #          |                               v
    #          |                    4. Build TopUp(PENDING) + key
    #          |                       Generate candidate Payment ID
    #          |                               |
    #          |                               v
    #          |                    5. create_once(): INSERT
    #          |                       Unique (user_id, idempotency_key)
    #          |                               |
    #          |                       +-------+-------------------+
    #          |                       |                           |
    #          |                       v                           v
    #          |                  Insert succeeds           Concurrent duplicate
    #          |                  Use inserted TopUp        Use winning TopUp
    #          |                       |                    and its saved IDs
    #          |                       |                           |
    #          +-----------------------+---------------------------+
    #                                  |
    #                                  v
    # 6. _check_same_request(): compare saved wallet_id + amount_minor
    #                                  |
    #                 +----------------+----------------+
    #                 |                                 |
    #                 v                                 v
    #           Different data                       Same data
    #           Raise conflict                          |
    #                 |                                 v
    #                 v                          7. COMMIT once
    #              ROLLBACK                      TopUp + key together
    #              Route returns 409                    |
    #                                                   v
    #                                            8. Return TopUp (201)
    #                                            Same saved IDs on retry;
    #                                            current status may change
    #
    # Any exception inside try -> ROLLBACK -> re-raise to the route.
    # A failed attempt never leaves a committed key without its TopUp.
    # A new key means a new request; another user's key is independent.
    def create_topup(
        self,
        user_id: int,
        wallet_id: int,
        amount: Decimal,
        idempotency_key: str,
    ) -> TopUp:
        # Normalize money: 3, 3.0 and 3.00 all mean 300 minor units.
        if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999999.99"):
            raise ValueError("Invalid top up amount")
        minor_amount = amount * Decimal("100")
        if minor_amount != minor_amount.to_integral_value():
            raise ValueError("Invalid top up amount")
        amount_minor = int(minor_amount)
        if not idempotency_key.strip() or len(idempotency_key) > 128:
            raise ValueError("Invalid idempotency key")

        try:
            # Check retries before current wallet rules: return the existing result.
            topup = self.topup_repository.get_by_key(user_id, idempotency_key)
            if topup is None:
                topup = self._create_new_topup(
                    user_id, wallet_id, amount_minor, idempotency_key,
                )
            self._check_same_request(topup, wallet_id, amount_minor)
            self.db.commit()
            return topup
        except Exception:
            self.db.rollback()
            raise

    def _check_same_request(self, topup: TopUp, wallet_id: int, amount_minor: int) -> None:
        # Both request fields are stored already, so no separate hash is needed.
        if topup.wallet_id != wallet_id or topup.amount_minor != amount_minor:
            raise TopUpIdempotencyConflictError("Idempotency key used with different data")

    def _create_new_topup(
        self, user_id: int, wallet_id: int, amount_minor: int, idempotency_key: str,
    ) -> TopUp:
        # Only a new request needs wallet validation and a new candidate Payment ID.
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

        topup = TopUp(
            user_id=user_id,
            wallet_id=wallet.id,
            amount_minor=amount_minor,
            currency=wallet.currency,
            moyasar_payment_id=str(uuid4()),
            idempotency_key=idempotency_key,
            status=TopUpStatus.PENDING,
        )
        # A competing request may win; return its saved ID, never the unused candidate.
        return self.topup_repository.create_once(topup)

    # Return only the owner's TopUp so the mobile can check its current status.
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
