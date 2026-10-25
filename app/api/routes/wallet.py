from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.wallet import CreateWalletRequest, WalletResponse
from app.services.wallet_service import WalletService
from app.api.routes.dependencies import get_current_user


router = APIRouter(
    prefix="/wallets",
    tags=["Wallets"],
)


#POST /wallets
@router.post(
    "",
    response_model=WalletResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_wallet(
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

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


#GET /wallets
@router.get(
    "",
    response_model=list[WalletResponse],
)
async def get_wallets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = WalletService(db)

    return service.get_user_wallets(
        user_id=current_user.id
    )

#GET /wallets/{wallet_id}
@router.get(
    "/{wallet_id}",
    response_model=WalletResponse,
)
async def get_wallet(
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

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )