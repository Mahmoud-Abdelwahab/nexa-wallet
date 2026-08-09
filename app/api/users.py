from fastapi import APIRouter, Depends

from app.api.dependencies import get_current_user
from app.models.user import User

router = APIRouter(
    prefix="/users",
    tags=["Users"],
)

# this means the Fastapi before it call get_me it call first get_current_user first to validate user token and get the user from the database and then pass it to get_me function as current_user parameter. if the token is invalid or expired it will raise an HTTPException with status code 401 Unauthorized.
@router.get("/me")
def get_me(
    current_user: User = Depends(get_current_user),
):
    return current_user
