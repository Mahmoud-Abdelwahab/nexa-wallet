from datetime import date

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.wallet import Wallet
from app.repositories import UserRepository, WalletRepository
from app.core.security import hash_password


class AuthService:

    def __init__(self, db: Session):
        self.db = db
        self.user_repository = UserRepository(db)
        self.wallet_repository = WalletRepository(db)

    def register_user(
        self,
        username: str,
        email: str,
        password: str,
        mobile: str,
        date_of_birth: date,
    ) -> User:

        # Check if username or email is already taken
        if self.user_repository.get_by_username(username):
            raise ValueError("Username already taken")

        if self.user_repository.get_by_email(email):
            raise ValueError("Email already registered")

        # Hash password before storing it
        password_hash = hash_password(password)

        user = User(
            username=username,
            email=email,
            password_hash=password_hash,
            mobile=mobile,
            date_of_birth=date_of_birth,
        )

        try:
            # Add user to the current transaction
            self.user_repository.create(user)

            # Send INSERT to DB so the generated user.id is available
            self.db.flush()

            # Create default AED wallet for the new user
            wallet = Wallet(
                user_id=user.id,
                currency="AED",
                balance=0,
            )

            self.wallet_repository.create(wallet)

            # Commit User + Wallet together
            self.db.commit()

            return user

        except Exception:
            # Rollback everything if any operation fails
            self.db.rollback()
            raise