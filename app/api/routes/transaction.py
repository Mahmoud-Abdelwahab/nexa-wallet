from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.routes.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.transaction import TransactionResponse
from app.services.transaction_service import TransactionService


router = APIRouter(
    prefix="/transactions",
    tags=["Transactions"],
)


@router.get(
    "",
    response_model=list[TransactionResponse],
)
async def get_transactions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = TransactionService(db)

    return service.get_user_transactions(current_user.id)


@router.get(
    "/{transaction_id}",
    response_model=TransactionResponse,
)
async def get_transaction(
    transaction_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = TransactionService(db)

    try:
        return service.get_transaction(
            transaction_id=transaction_id,
            user_id=current_user.id,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
