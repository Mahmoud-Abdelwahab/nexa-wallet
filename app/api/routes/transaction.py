from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.api.routes.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.transaction import TransactionResponse
from app.services.transaction_service import TransactionService


# These endpoints use synchronous SQLAlchemy; FastAPI runs them in its thread pool.
router = APIRouter(
    prefix="/transactions",
    tags=["Transactions"],
)


@router.get(
    "",
    response_model=list[TransactionResponse],
)
def get_transactions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = TransactionService(db)

    return service.get_user_transactions(current_user.id)


@router.get(
    "/{transaction_id}",
    response_model=TransactionResponse,
)
def get_transaction(
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

    except BusinessError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
