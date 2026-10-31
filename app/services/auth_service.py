from datetime import date

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.errors import BusinessError
from app.mappers.user_mapper import UserMapper
from app.models.user import User
from app.models.wallet import Wallet
from app.repositories import UserRepository, WalletRepository
from app.core.security import create_access_token, hash_password, verify_password
from app.infrastructure.redis.refresh_token_redis_store import RefreshTokenRedisStore
from app.schemas.authentication import AuthResponse
from app.schemas.refresh_session import RefreshSession
from app.services.refresh_token_service import RefreshTokenService


# prevent Timing Side-Channel
# dummy passsword we use to prevent timing attacks when the user does not exist. This ensures that the time taken for the operation is consistent, regardless of whether the user exists or not.
DUMMY_PASSWORD_HASH = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeg6Lruj3vjPGga31lW"

# AuthService keeps the original entry-point names and business flow.
# Helpers ending in _sync run the blocking steps in a worker thread.
# Redis steps stay async and run through await.
#
# register_user / login_user
#             |
#             v
# Save user / verify password (worker thread)
#             |
#             v
# _build_auth_response
#             |
#             +--> RefreshTokenService: generate token + session
#             |
#             +--> RefreshTokenRedisStore: await session storage
#             |
#             v
# Return access JWT + refresh token + user


class AuthService:

    def __init__(
        self,
        db: Session,
        refresh_token_service: RefreshTokenService,
        refresh_token_store: RefreshTokenRedisStore,
    ):
        self.db = db

        self.user_repository = UserRepository(db)
        self.wallet_repository = WalletRepository(db)

        self.refresh_token_service = refresh_token_service
        self.refresh_token_store = refresh_token_store

    # register_user
    #      |
    #      v
    # Check duplicates -> hash password -> create User + AED Wallet
    #      |              (all inside _register_user_sync)
    #      v
    # Commit User + Wallet together
    #      |
    #      v
    # _build_auth_response -> store Redis session -> return AuthResponse
    async def register_user(
        self,
        username: str,
        email: str,
        password: str,
        mobile: str,
        date_of_birth: date,
    ) -> AuthResponse:
        # Keep SQLAlchemy and password hashing off the event loop.
        user = await run_in_threadpool(
            self._register_user_sync,
            username,
            email,
            password,
            mobile,
            date_of_birth,
        )
        return await self._build_auth_response(user)

    def _register_user_sync(
        self,
        username: str,
        email: str,
        password: str,
        mobile: str,
        date_of_birth: date,
    ) -> User:
        email = email.lower()

        # Use one message so we do not identify which contact belongs to an account.
        if self.user_repository.get_by_mobile(mobile):
            raise BusinessError("Unable to register with these details")

        if self.user_repository.get_by_email(email):
            raise BusinessError("Unable to register with these details")

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

        # The async caller creates the Redis session only after this commit.
        return user

    # login_user
    #      |
    #      v
    # Find user -> verify password (inside _login_user_sync)
    #      |
    #      v
    # _build_auth_response -> store Redis session -> return AuthResponse
    async def login_user(self, email: str, password: str) -> AuthResponse:
        # The database query and bcrypt verification are both synchronous.
        user = await run_in_threadpool(self._login_user_sync, email, password)
        return await self._build_auth_response(user)

    def _login_user_sync(self, email: str, password: str) -> User:
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
            raise BusinessError("Invalid email or password")

        return user

    # _build_auth_response
    #      |
    #      v
    # Generate refresh session + build AuthResponse (worker thread)
    #      |
    #      v
    # Await storing the session in Redis
    #      |
    #      v
    # Return AuthResponse
    async def _build_auth_response(self, user: User) -> AuthResponse:
        # A committed ORM object may reload attributes on access. Read and
        # serialize it in a worker too, before awaiting Redis on the event loop.
        response, token_hash, session = await run_in_threadpool(
            self._build_auth_response_sync, user
        )

        await self.refresh_token_store.store_session(
            token_hash=token_hash,
            session=session,
        )
        return response

    def _build_auth_response_sync(
        self,
        user: User,
    ) -> tuple[AuthResponse, str, RefreshSession]:

        (
            refresh_token,
            token_hash,
            session,
        ) = self.refresh_token_service.create_refresh_session(user.id)

        response = self._create_auth_response_sync(user, refresh_token)
        return response, token_hash, session

    def _create_auth_response_sync(
        self,
        user: User,
        refresh_token: str,
    ) -> AuthResponse:
        # Called in a worker: all ORM attribute reads finish before returning.
        access_token = create_access_token(
            data={
                "sub": str(user.id),
                "token_version": user.token_version,
            }
        )
        return AuthResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=UserMapper.to_response(user),
        )

    # refresh_access_token
    #      |
    #      v
    # Hash incoming token -> await Redis session lookup
    #      |
    #      v
    # Check expiry -> load user (worker thread)
    #      |
    #      v
    # Generate replacement token with the same session identity
    #      |
    #      v
    # Await atomic Redis rotation: consume old token; store new token
    #      |
    #      v
    # Create access JWT + user response (worker thread) -> return
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
            raise BusinessError("Invalid refresh token")

        # 3. Calculate the new Redis TTL without exceeding absolute expiration.
        ttl_seconds = (
            self.refresh_token_service.calculate_ttl_seconds(
                session.absolute_expires_at
            )
        )

        if ttl_seconds <= 0:
            await self.refresh_token_store.revoke_session(
                old_token_hash,
                user_id=session.user_id,
            )
            raise BusinessError("Invalid refresh token")

        # 4. Find the user attached to the refresh session.
        user = await run_in_threadpool(
            self.user_repository.get_by_id,
            session.user_id
        )

        if user is None:
            raise BusinessError("Invalid refresh token")

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
            raise BusinessError("Invalid refresh token")

        # 7. Create a new access token and return both new tokens to the client.
        return await run_in_threadpool(
            self._create_auth_response_sync, user, new_refresh_token
        )

    # logout -> hash refresh token -> await revoking this session only
    async def logout(
        self,
        refresh_token: str,
        user_id: int,
    ) -> None:

        token_hash = self.refresh_token_service.hash_refresh_token(
            refresh_token
        )

        await self.refresh_token_store.revoke_session(
            token_hash=token_hash,
            user_id=user_id,
        )

    # logout_all
    #      |
    #      v
    # Await revocation of all Redis refresh sessions
    #      |
    #      v
    # Increment token_version -> commit (inside _logout_all_sync)
    async def logout_all(
        self,
        user: User,
    ) -> None:

        # The sync authentication dependency has already loaded this user's id.
        await self.refresh_token_store.revoke_all_sessions(
            user.id
        )

        # Redis stays async; the SQLAlchemy mutation and commit run in a worker.
        await run_in_threadpool(self._logout_all_sync, user)

    def _logout_all_sync(self, user: User) -> None:
        # Increment the token version to invalidate all existing access tokens for this user
        user.token_version += 1
        #! why we didn't do this in logout() because we need to logout once device only and this token_vesion related to the user level so if we incremant it all other devices will be logged out and we don't want that in logout() but in logout_all() we want to logout all devices so we increment it here

        self.db.commit()
