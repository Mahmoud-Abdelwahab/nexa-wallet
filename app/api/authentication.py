from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.authentication import (
    AuthResponse,
    LoginRequest,
    RegisterRequest,
)
from app.schemas.refresh_token_request import RefreshTokenRequest
from app.services.auth_service import AuthService
from app.services.refresh_token_service import RefreshTokenService
from app.infrastructure.redis.refresh_token_redis_store import RefreshTokenStore
from app.core.redis import redis_client

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"],
)


def get_auth_service(
    db: Session = Depends(get_db),
) -> AuthService:

    refresh_token_service = RefreshTokenService()

    refresh_token_store = RefreshTokenStore(redis_client)

    return AuthService(
        db=db,
        refresh_token_service=refresh_token_service,
        refresh_token_store=refresh_token_store,
    )


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    request: RegisterRequest,
    service: AuthService = Depends(get_auth_service),
):
    try:
        return await service.register_user(
            username=request.username,
            email=request.email,
            password=request.password,
            mobile=request.mobile,
            date_of_birth=request.date_of_birth,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/login",
    response_model=AuthResponse,
)
async def login(
    request: LoginRequest,
    service: AuthService = Depends(get_auth_service),
):
    try:
        return await service.login_user(
            email=request.email,
            password=request.password,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/refresh",
    response_model=AuthResponse,
)
async def refresh(
    request: RefreshTokenRequest,
    service: AuthService = Depends(get_auth_service),
):
    try:
        return await service.refresh_access_token(
            request.refresh_token
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )
