# The router passes schema-validated requests to the appropriate service:
# Login/Register -> AuthService -> token pair + new session
# GET sessions   -> get_current_user -> DeviceSessionsService -> device list
# DELETE sid     -> get_current_user -> DeviceSessionsService -> revoke one session
# get_current_user checks sid, so revoked sessions cannot use the management endpoints either.
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from redis.exceptions import RedisError
from typing import Annotated
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.api.routes.dependencies import get_current_user, SESSION_REVOKED_DETAIL
from app.database import get_db
from app.schemas.authentication import (
    AuthResponse,
    LoginRequest,
    RegisterRequest,
)
from app.schemas.refresh_token_request import RefreshTokenRequest
from app.services.auth_service import AuthService
from app.services.refresh_token_service import RefreshTokenService
from app.infrastructure.redis.refresh_token_redis_store import RefreshTokenRedisStore
from app.core.redis import redis_client
from app.models.user import User
from app.schemas.device_session import DeviceSessionResponse
from app.services.device_sessions_service import DeviceSessionsService

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"],
)


def get_auth_service(
    db: Session = Depends(get_db),
) -> AuthService:

    refresh_token_service = RefreshTokenService()

    refresh_token_store = RefreshTokenRedisStore(redis_client)

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
            # Optional device metadata is length-limited and platform-validated by the request schema.
            device_name=request.device_name,
            platform=request.platform,
        )

    except BusinessError as e:
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
            # Save the device label for display; it does not establish identity or ownership.
            device_name=request.device_name,
            platform=request.platform,
        )

    except BusinessError as e:
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
            request.refresh_token,
        )

    except BusinessError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            # The client clears tokens on this code instead of repeatedly retrying an invalid refresh.
            # Covers revoked, expired, invalid and previously consumed refresh tokens.
            detail=SESSION_REVOKED_DETAIL,
        )

    except RedisError:
        # A temporary outage must not return SESSION_REVOKED and force users to log out.
        raise HTTPException(503, {
            "code": "AUTH_SERVICE_UNAVAILABLE",
            "message": "Authentication is temporarily unavailable. Please retry.",
        }) from None


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def logout(
    request: RefreshTokenRequest,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
):
    await service.logout(
        refresh_token=request.refresh_token,
        user_id=current_user.id,
        # The dependency provided a verified sid; revoke the requesting session itself.
        session_id=http_request.state.session_id,
    )


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def logout_all(
    current_user: User = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
):
    await service.logout_all(current_user)


@router.get("/sessions", response_model=list[DeviceSessionResponse])
async def get_sessions(request: Request, current_user: User = Depends(get_current_user)):
    # Use the authenticated account's ID and version; the client cannot select another user.
    # response_model exposes display fields only, without refresh tokens or hashes.
    try:
        return await DeviceSessionsService(redis_client).list_sessions(
            current_user.id, current_user.token_version, request.state.session_id
        )
    except RedisError:
        raise HTTPException(503, "Sessions are temporarily unavailable") from None


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    current_user: User = Depends(get_current_user),
):
    # DELETE /auth/sessions/S1 searches only within the authenticated user's sessions.
    # Hiding a mobile button is insufficient; enforce current-session deletion protection on the server.
    # Use sid from the verified JWT; signing out the current session belongs to logout.
    if session_id == request.state.session_id:
        raise HTTPException(400, {
            "code": "CURRENT_SESSION_DELETE_FORBIDDEN",
            "message": "Cannot delete your current session. Use logout instead.",
        })
    try:
        revoked = await DeviceSessionsService(redis_client).revoke_session(
            current_user.id, session_id
        )
    except RedisError:
        raise HTTPException(503, "Sessions are temporarily unavailable") from None
    if not revoked:
        raise HTTPException(404, "Session not found")
