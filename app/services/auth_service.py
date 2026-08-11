from datetime import date

from sqlalchemy.orm import Session

from app.mappers.user_mapper import UserMapper
from app.models.user import User
from app.models.wallet import Wallet
from app.repositories import UserRepository, WalletRepository
from app.core.security import create_access_token, hash_password, verify_password
from app.schemas.authentication import AuthResponse, UserResponse

# prevent Timing Side-Channel
# dummy passsword we use to prevent timing attacks when the user does not exist. This ensures that the time taken for the operation is consistent, regardless of whether the user exists or not.
DUMMY_PASSWORD_HASH = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeg6Lruj3vjPGga31lW"


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
    ) -> AuthResponse:
        email = email.lower()

        # Check if username or email is already taken
        if self.user_repository.get_by_mobile(mobile):
            raise ValueError("Mobile number already registered")

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

        except Exception:
            # Rollback everything if any operation fails
            self.db.rollback()
            raise

        # Create token ONLY after successful commit
        return self._build_auth_response(user)

    def login_user(self, email: str, password: str) -> AuthResponse:
        email = email.lower()

        user = self.user_repository.get_by_email(email)

        if user:
            # If the user exists, we verify the actual password hash
            is_password_valid = verify_password(password, user.password_hash)
        else:
            # If the user does not exist, we still call verify_password with the dummy hash to prevent timing attacks
            # Note: This is a security measure to prevent timing attacks. We always call verify_password, even if the user doesn't exist, to ensure that the time taken for the operation is consistent.
            verify_password(password, DUMMY_PASSWORD_HASH)
            is_password_valid = False

        if not user or not is_password_valid:
            raise ValueError("Invalid email or password")

        return self._build_auth_response(user)

    def _build_auth_response(self, user: User) -> AuthResponse:
        access_token = create_access_token(
            data={"sub": str(user.id)}
        )

        return AuthResponse(
            access_token=access_token,
            token_type="bearer",
            user=UserMapper.to_response(user),
        )
