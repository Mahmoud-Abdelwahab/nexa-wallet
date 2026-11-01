from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.database import get_db
from app.models.user import User
from app.schemas.ledger import LedgerEntryResponse
from app.schemas.wallet import CreateWalletRequest, WalletResponse
from app.services.wallet_service import WalletService
from app.api.routes.dependencies import get_current_user


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
