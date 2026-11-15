from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.core.redis import redis_client
from app.database import get_db
from app.schemas.password_reset import (
    ForgotPasswordRequest,
    PasswordResetResponse,
    ResetPasswordRequest,
)
from app.services.password_reset_service import (
    FORGOT_PASSWORD_MESSAGE,
    PasswordResetService,
)


router = APIRouter(prefix="/auth", tags=["Authentication"])


def get_password_reset_service(db: Session = Depends(get_db)) -> PasswordResetService:
    return PasswordResetService(db, redis_client)


@router.post("/forgot-password", response_model=PasswordResetResponse)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    service: PasswordResetService = Depends(get_password_reset_service),
):
    try:
        await service.forgot_password(
            str(payload.email),
            request.client.host if request.client else "unknown",
            background_tasks,
        )
    except (RedisError, SQLAlchemyError):
        raise HTTPException(503, "Password reset is temporarily unavailable. Please try again.") from None
    return PasswordResetResponse(message=FORGOT_PASSWORD_MESSAGE)


@router.post("/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    payload: ResetPasswordRequest,
    service: PasswordResetService = Depends(get_password_reset_service),
):
    try:
        await service.reset_password(payload.token, payload.new_password)
    except BusinessError as error:
        raise HTTPException(400, str(error)) from None
    except (RedisError, SQLAlchemyError):
        # GETDEL may already have consumed this token. Ask for a new one rather
        # than restoring a token after an ambiguous commit or Redis failure.
        raise HTTPException(
            503, "Unable to complete password reset. Request a new reset email and try again."
        ) from None
    return PasswordResetResponse(message="Password reset successfully. Please log in again.")
