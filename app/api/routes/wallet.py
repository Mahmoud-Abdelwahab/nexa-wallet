import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import BusinessError
from app.core.redis import redis_client
from app.database import get_db
from app.models.user import User
from app.schemas.ledger import LedgerEntryResponse
from app.schemas.recipient import RecipientLookupRequest, RecipientLookupResponse
from app.schemas.wallet import CreateWalletRequest, WalletResponse
from app.services.recipient_rate_limit import allow_recipient_lookup
from app.services.recipient_service import RecipientService
from app.services.wallet_service import WalletService
from app.api.routes.dependencies import get_current_user


logger = logging.getLogger(__name__)


# These endpoints use synchronous SQLAlchemy; FastAPI runs them in its thread pool.
router = APIRouter(
    prefix="/wallets",
    tags=["Wallets"],
)


# POST /wallets
@router.post(
    "",
    response_model=WalletResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_wallet(
    request: CreateWalletRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = WalletService(db)

    try:
        return service.create_wallet(
            user_id=current_user.id,
            currency=request.currency,
        )

    except BusinessError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


# GET /wallets
@router.get(
    "",
    response_model=list[WalletResponse],
)
def get_wallets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = WalletService(db)

    return service.get_user_wallets(
        user_id=current_user.id
    )


# GET /wallets/{wallet_id}
@router.get(
    "/{wallet_id}",
    response_model=WalletResponse,
)
def get_wallet(
    wallet_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = WalletService(db)

    try:
        return service.get_wallet(
            wallet_id=wallet_id,
            user_id=current_user.id,
        )

    except BusinessError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


# Request
#    ↓
# FastAPI يسلّم التنفيذ لـworker thread
#    ↓
# الـthread ينفّذ الاستعلام وينتظر الداتابيز
#    ↓
# الـevent loop يفضل متاح لطلبات تانية
@router.get(
    "/{wallet_id}/entries",
    response_model=list[LedgerEntryResponse],
)
# Use def because our SQLAlchemy Session is synchronous. FastAPI runs this
# sync endpoint in a thread pool so database calls do not block the event loop.
# A synchronous call inside async def would still block unless offloaded.
def get_wallet_statement(
    wallet_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # FastAPI validates pagination and provides the authenticated user.
    service = WalletService(db)

    try:
        # The service checks ownership before returning ledger history.
        return service.get_wallet_statement(
            wallet_id=wallet_id,
            user_id=current_user.id,
            limit=limit,
            offset=offset,
        )

    except BusinessError as e:
        # Use the same 404 for missing wallets and wallets owned by others.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


async def enforce_recipient_rate_limit(
    current_user: User = Depends(get_current_user),
) -> None:
    daily_limit = settings.RECIPIENT_LOOKUP_DAILY_LIMIT
    try:
        allowed = await allow_recipient_lookup(redis_client, current_user.id, daily_limit)
    except RedisError:
        logger.warning(
            "Recipient lookup rejected user_id=%s reason=rate_limit_unavailable",
            current_user.id,
        )
        raise HTTPException(503, "Recipient lookup is temporarily unavailable") from None
    if not allowed:
        logger.warning(
            "Recipient lookup rejected user_id=%s reason=rate_limit", current_user.id
        )
        raise HTTPException(429, "Too many recipient lookups. Please try again later.")


@router.post(
    "/recipient/resolve",
    response_model=RecipientLookupResponse,
    dependencies=[Depends(enforce_recipient_rate_limit)],
)
def resolve_recipient(
    payload: RecipientLookupRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return RecipientService(db).resolve(
            current_user_id=current_user.id,
            sender_wallet_id=payload.sender_wallet_id,
            recipient_wallet_id=payload.recipient_wallet_id,
        )
    except HTTPException as error:
        # Record rejected attempts without logging names, request bodies or tokens.
        logger.warning(
            "Recipient lookup rejected user_id=%s status=%s",
            current_user.id,
            error.status_code,
        )
        raise
