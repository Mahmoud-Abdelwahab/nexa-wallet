from sqlalchemy.orm import Session

from app.models.user import User
from app.repositories import UserRepository
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