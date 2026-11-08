from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.api.routes.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.topup import CreateTopUpRequest, TopUpResponse
from app.services.topup_service import TopUpService, TopUpIdempotencyConflictError


router = APIRouter(
    prefix="/topups",
    tags=["Top Ups"],
)

"""
the client will use the returned givenID to create a payment request to Moyasar, for example in Swift:
let request = try PaymentRequest(
    apiKey: "pk_test_...",
    amount: topUp.amountMinor,
    currency: topUp.currency,
    description: "Nexa Wallet Top Up",
    givenID: topUp.moyasarPaymentId
)
"""
@router.post(
    "",
    response_model=TopUpResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_topup(
    request: CreateTopUpRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = TopUpService(db)

    try:
        return service.create_topup(
            user_id=current_user.id,
            wallet_id=request.wallet_id,
            amount=request.amount,
            idempotency_key=idempotency_key,
        )

    except TopUpIdempotencyConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))

    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Mobile checks its request -> verify ownership -> return the stored status.
@router.get(
    "/{topup_id}",
    response_model=TopUpResponse,
)
def get_topup(
    topup_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = TopUpService(db)

    try:
        return service.get_topup(
            topup_id=topup_id,
            user_id=current_user.id,
        )

    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
