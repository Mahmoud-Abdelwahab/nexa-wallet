from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.mappers.user_mapper import UserMapper
from app.models.user import User
from app.models.wallet import Wallet
from app.repositories import UserRepository, WalletRepository
from app.core.security import create_access_token, hash_password, verify_password
from app.infrastructure.redis.refresh_token_redis_store import RefreshTokenStore
from app.schemas.authentication import AuthResponse, UserResponse
from app.schemas.refresh_session import RefreshSession
from app.services.refresh_token_service import RefreshTokenService


# prevent Timing Side-Channel
# dummy passsword we use to prevent timing attacks when the user does not exist. This ensures that the time taken for the operation is consistent, regardless of whether the user exists or not.
DUMMY_PASSWORD_HASH = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeg6Lruj3vjPGga31lW"

# AuthService
#     │
#     ├── asks RefreshTokenService
#     │        "create me a session"
#     │
#     └── asks RefreshTokenStore
#              "save this session"


class AuthService:

    def __init__(
        self,
        db: Session,
        refresh_token_service: RefreshTokenService,
        refresh_token_store: RefreshTokenStore,
    ):
        self.db = db

        self.user_repository = UserRepository(db)
        self.wallet_repository = WalletRepository(db)

        self.refresh_token_service = refresh_token_service
        self.refresh_token_store = refresh_token_store

    async def register_user(
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
        return await self._build_auth_response(user)

    async def login_user(self, email: str, password: str) -> AuthResponse:
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

        return await self._build_auth_response(user)

    async def _build_auth_response(self, user: User) -> AuthResponse:
        access_token = create_access_token(
            data={
                "sub": str(user.id),
                "token_version": user.token_version,
            }
        )

        (
            refresh_token,
            token_hash,
            session,
        ) = self.refresh_token_service.create_refresh_session(user.id)

        await self.refresh_token_store.store_session(
            token_hash=token_hash,
            session=session,
        )

        return AuthResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=UserMapper.to_response(user),
        )

#! 1- take refresh token from request 
#  2- hash it and get the session related to id from redis 
#  3- calculate the new absolute_expires_at [ not new ] we calcuate the remaining days from eg 90 day
#  4- then get user to  create new access token using it's id and create rotated refresh token useing same userid and calcualted absolute_expires_at and new raw refresh token 
# we ca use the userID from the session that we get from redis but his not better way foe security u need to check if the user still exists or you may need to check if user status = active or not blocked to not generate token from him 
#  5- create new rotated session with same old idenetiy and new absolute_expires_at and retrun new refersh raw and hashed ans session
#! 6- importatn step to rotate the token in redis, get old refresh hash then delete it , then add the new one and return the result if success 
#  7- create normal access token and create the same  AuthResponse and send it to the client with new access token and refresh token also 
#  8- refresh Token used once only same as access token
    async def refresh_access_token(
        self,
        refresh_token: str,
    ) -> AuthResponse:

        # 1. Hash the raw refresh token so the Redis lookup uses the stored key.
        old_token_hash = (
            self.refresh_token_service.hash_refresh_token(
                refresh_token
            )
        )

        # 2. Load the old session. A missing session means the token is invalid,
        #    expired from Redis, revoked, or already used.
        session = await self.refresh_token_store.get_session(
            old_token_hash
        )

        if session is None:
            raise ValueError("Invalid refresh token")

        # 3. Calculate the new Redis TTL without exceeding absolute expiration.
        ttl_seconds = (
            self.refresh_token_service.calculate_ttl_seconds(
                session.absolute_expires_at
            )
        )

        if ttl_seconds <= 0:
            await self.refresh_token_store.revoke_session(
                old_token_hash
            )
            raise ValueError("Refresh session expired")

        # 4. Find the user attached to the refresh session.
        user = self.user_repository.get_by_id(
            session.user_id
        )

        if user is None:
            raise ValueError("User not found")

        # 5. Generate a new refresh token while keeping the same session identity
        #    and absolute expiration.
        new_refresh_token, new_token_hash, new_session = (
            self.refresh_token_service.create_rotated_token(session)
        )

        # 6. Atomically delete the old token and save the new one. This prevents
        #    the old refresh token from being reused during concurrent requests.
        success = await self.refresh_token_store.rotate_session(
            old_token_hash=old_token_hash,
            new_token_hash=new_token_hash,
            session=new_session,
            ttl_seconds=ttl_seconds,
        )

        if not success:
            raise ValueError("Invalid refresh token")

        # 7. Create a new access token and return both new tokens to the client.
        access_token = create_access_token(
            data={
                "sub": str(user.id),
                "token_version": user.token_version,
            }
        )

        return AuthResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
            token_type="bearer",
            user=UserMapper.to_response(user),
        )
            