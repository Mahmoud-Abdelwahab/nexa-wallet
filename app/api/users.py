from fastapi import APIRouter, Depends, HTTPException, status
from app.api.dependencies import get_current_user
from app.mappers.user_mapper import UserMapper
from app.models.user import User
from app.schemas.authentication import UserResponse
from app.schemas.updateUserRequest import UpdateUserRequest
from app.services.user_service import UserService
from sqlalchemy.orm import Session
from app.database import get_db

router = APIRouter(
    prefix="/users",
    tags=["Users"],
)

# this means the Fastapi before it call get_me it call first get_current_user first to validate user token and get the user from the database and then pass it to get_me function as current_user parameter. if the token is invalid or expired it will raise an HTTPException with status code 401 Unauthorized.
@router.get(
        "/me",
        response_model=UserResponse,)
def get_me(
    current_user: User = Depends(get_current_user),
):
     return UserMapper.to_response(current_user) # mapping user model to user response schema to return only the required fields in the response.

@router.patch(
    "/me",
    response_model=UserResponse,
)
def update_me(
    request: UpdateUserRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = UserService(db)

    try:
        user = service.update_profile(
            user=current_user,
            request=request,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return UserMapper.to_response(user)