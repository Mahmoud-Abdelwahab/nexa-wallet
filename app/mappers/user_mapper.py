from app.models.user import User
from app.schemas.authentication import UserResponse


class UserMapper:

    @staticmethod
    def to_response(user: User) -> UserResponse:
        return UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            mobile=user.mobile,
        )