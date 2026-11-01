from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.api.routes.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.transfer import TransferRequest, TransferResponse
from app.services.transfer_service import IdempotencyConflictError, TransferService


# These endpoints use synchronous SQLAlchemy; FastAPI runs them in its thread pool.
router = APIRouter(
    prefix="/transfers",
    tags=["Transfers"],
)


@router.post(
    "",
    response_model=TransferResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_transfer(
    request: TransferRequest,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=1, max_length=128
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Retries must send the same key and the same request data.
    service = TransferService(db)

    try:
        return service.transfer(
            user_id=current_user.id,
            sender_wallet_id=request.sender_wallet_id,
            receiver_wallet_id=request.receiver_wallet_id,
            amount=request.amount,
            idempotency_key=idempotency_key,
            message=request.message,
        )

    except IdempotencyConflictError:
        # A key can be retried only with the original request data.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key used with different data",
        )
    except BusinessError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

#     POST /transfers
# {
#   "sender_wallet_id": 1,
#   "receiver_wallet_id": 2,
#   "amount": 100
# }
#         ↓
# Access Token
#         ↓
# get_current_user()
#         ↓
# user_id
#         ↓
# TransferService
#         ↓
# reserve Idempotency-Key (or return the saved result)
#         ↓
# lock wallets
#         ↓
# validations
#         ↓
# Transaction
#         ↓
# DEBIT + CREDIT Ledger
#         ↓
# update balances
#         ↓
# COMMIT
