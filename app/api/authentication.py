from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.authentication import AuthResponse, LoginRequest, RegisterRequest
from app.services.auth_service import AuthService

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"],
)


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    request: RegisterRequest,
    db: Session = Depends(get_db),
):
    service = AuthService(db)

    try:
        return service.register_user(
            username=request.username,
            email=request.email,
            password=request.password,
            mobile=request.mobile,
            date_of_birth=request.date_of_birth,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/login",
    response_model=AuthResponse,
)
def login(
    request: LoginRequest,
    db: Session = Depends(get_db),
):
    service = AuthService(db)

    try:
        return service.login_user(
            email=request.email,
            password=request.password,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))