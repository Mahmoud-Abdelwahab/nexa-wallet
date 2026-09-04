from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.user import User
from app.repositories import UserRepository
from app.schemas.changePasswordRequest import ChangePasswordRequest
from app.schemas.updateUserRequest import UpdateUserRequest


class UserService:

    def __init__(self, db: Session):
        self.db = db
        self.user_repository = UserRepository(db)

    def update_profile(
        self,
        user: User,
        request: UpdateUserRequest,
    ) -> User:

        if not request.model_fields_set:
            raise ValueError("At least one field must be provided")

        if "username" in request.model_fields_set:
            if request.username is not None:
                user.username = request.username

        if "mobile" in request.model_fields_set:
            if request.mobile is not None:
               existing_user = self.user_repository.get_by_mobile(
                request.mobile
               )

               if existing_user and existing_user.id != user.id:
                   raise ValueError("Mobile number already registered")
               user.mobile = request.mobile

        self.db.commit()
        # refresh the user instance to get the latest data from the database after commit, this is important because if we don't refresh the user instance it will still have the old data in memory and when we return it to the client it will return the old data instead of the updated data.
        self.db.refresh(user) 

        return user


    def change_password(
        self,
        user: User,
        request: ChangePasswordRequest,
    ) -> None:

        if not verify_password(
            request.current_password,
            user.password_hash,
        ):
            raise ValueError("Current password is incorrect")

        if request.new_password != request.confirm_password:
            raise ValueError("New Passwords and confirm password do not match")

        if request.current_password == request.new_password:
            raise ValueError("New password cannot be the same as the current password")
        
        user.password_hash = hash_password(
            request.new_password
        )
        # Increment token_version to invalidate existing JWT tokens
        # once password change is successfull, then the jwt token will be invalidated because the token_version in the database will be different from the token_version in the jwt token, so the user will have to login again to get a new jwt token with the new token_version.
        user.token_version += 1 
        self.db.commit()