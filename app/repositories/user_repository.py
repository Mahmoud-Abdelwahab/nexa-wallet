from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User


class UserRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_by_username(self, username: str) -> User | None:
        statement = select(User).where(User.username == username)
        return self.db.scalar(statement)

    def get_by_email(self, email: str) -> User | None:
        statement = select(User).where(User.email == email)
        return self.db.scalar(statement)

    def get_by_id(self, user_id: int) -> User | None:
        # statement = select(User).where(User.id == user_id)
        # return self.db.scalar(statement)
         return self.db.get(User, user_id)  # get method is more efficient than select statement for primary key lookups

    def get_by_mobile(self, mobile: str) -> User | None:
            statement = select(User).where(User.mobile == mobile)
            return self.db.scalar(statement)
    
    def create(self, user: User) -> User:
        self.db.add(user)
        return user