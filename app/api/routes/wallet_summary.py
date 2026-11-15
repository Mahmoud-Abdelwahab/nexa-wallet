"""Summary route must be registered BEFORE /wallets/{wallet_id}."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api.routes.dependencies import get_current_user
from app.core.enums import Currency
from app.core.redis import redis_client
from app.database import get_db
from app.integrations.frankfurter_client import ExchangeRateUnavailable, FrankfurterClient
from app.models.user import User
from app.repositories.wallet_repository import WalletRepository
from app.schemas.wallet_summary import WalletSummaryResponse
from app.services.wallet_summary_service import calculate_wallet_summary


router = APIRouter(prefix="/wallets", tags=["Wallets"])


@router.get("/summary", response_model=WalletSummaryResponse)
async def get_wallet_summary(
    base_currency: Currency = Currency.AED,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # This project uses a synchronous SQLAlchemy Session; do not block the
    # event loop with its database query inside an async route.
    wallets = await run_in_threadpool(
        WalletRepository(db).get_by_user_id, current_user.id,
    )
    needed = {wallet.currency for wallet in wallets if wallet.currency != base_currency}
    if not needed:
        rates = {}
    else:
        try:
            rates = await FrankfurterClient(redis_client).get_rates(
                base_currency, needed,
            )
        except ExchangeRateUnavailable:
            raise HTTPException(
                status_code=503,
                detail="Reference exchange rates are temporarily unavailable",
            )

    return calculate_wallet_summary(wallets, base_currency, rates)
