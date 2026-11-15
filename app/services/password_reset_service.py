import hashlib
import logging
import smtplib

from fastapi import BackgroundTasks
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.errors import BusinessError
from app.core.security import hash_password
from app.infrastructure.redis.refresh_token_redis_store import RefreshTokenRedisStore
from app.models.user import User
from app.schemas.password_reset import ResetPasswordRequest
from app.services.email_service import send_reset_email
from app.services.password_reset_token_store import (
    RESET_TTL_SECONDS,
    consume_reset_token,
    create_reset_token,
    reset_key,
)


logger = logging.getLogger(__name__)
FORGOT_PASSWORD_MESSAGE = "If the email exists, reset instructions will be sent."
INVALID_RESET_MESSAGE = "Invalid or expired reset token"

# Count requests for existing and unknown accounts alike. The counter and its
# expiry are set atomically, so interrupted requests cannot leave permanent keys.
RATE_LIMIT_SCRIPT = """
local email_count = redis.call('INCR', KEYS[1])
if email_count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local ip_count = redis.call('INCR', KEYS[2])
if ip_count == 1 then redis.call('EXPIRE', KEYS[2], ARGV[1]) end
if email_count > 3 or ip_count > 10 then return 0 end
return 1
"""
#* Below is the explanation of the above script in case you want to understand it better:
# local email_count = redis.call('INCR', KEYS[1])
# if email_count == 1 then
#     redis.call('EXPIRE', KEYS[1], ARGV[1])
# end

# local ip_count = redis.call('INCR', KEYS[2])
# if ip_count == 1 then
#     redis.call('EXPIRE', KEYS[2], ARGV[1])
# end

# if email_count > 3 or ip_count > 10 then
#     return 0
# end

# return 1
#*------- the blow is how the keys will look like in redis after running the above script
# password_reset_rate:email:<email_hash> → 2
# password_reset_rate:ip:<ip_hash>       → 5

class PasswordResetService:
    def __init__(self, db: Session, redis: Redis):
        self.db = db
        self.redis = redis
        self.refresh_token_store = RefreshTokenRedisStore(redis)

    async def forgot_password(
        self, email: str, client_ip: str, background_tasks: BackgroundTasks
    ) -> None:
        email = email.strip().lower()
        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()
        ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
        allowed = await self.redis.eval(
            RATE_LIMIT_SCRIPT,
            2,
            f"password_reset_rate:email:{email_hash}",
            f"password_reset_rate:ip:{ip_hash}",
            RESET_TTL_SECONDS,
        )
        # Suppressed requests get the same response as unknown accounts.
        if not allowed:
            return
        identity = await run_in_threadpool(self._find_identity_sync, email)
        if identity is None:
            return
        user_id, version = identity
        token = await create_reset_token(self.redis, user_id, version)
        # SMTP latency and failures must not disclose whether an account exists.
        # This task only uses scalar values, never a request's database session.
        background_tasks.add_task(self._deliver_email, email, token)

    def _find_identity_sync(self, email: str) -> tuple[int, int] | None:
        row = self.db.execute(
            select(User.id, User.token_version).where(User.email == email)
        ).first()
        return (row[0], row[1]) if row else None

    async def _deliver_email(self, email: str, token: str) -> None:
        try:
            await send_reset_email(email, token)
        except (OSError, smtplib.SMTPException):
            # Never log the token, recipient, SMTP credentials or exception body.
            logger.warning("Password reset email delivery failed")
            try:
                await self.redis.delete(reset_key(token))
            except RedisError:
                logger.warning("Undelivered password reset token cleanup failed")

    async def reset_password(self, token: str, new_password: str) -> None:
        # Validate before GETDEL even when called outside the HTTP route.
        request = ResetPasswordRequest(token=token, new_password=new_password)
        identity = await consume_reset_token(self.redis, request.token)
        if identity is None:
            raise BusinessError(INVALID_RESET_MESSAGE)
        user_id, version = identity
        await run_in_threadpool(
            self._reset_password_sync, user_id, version, request.new_password
        )
        try:
            await self.refresh_token_store.revoke_all_sessions(user_id)
        except RedisError:
            # The committed version change already rejects access tokens and
            # refresh sessions, including sessions created during cleanup.
            logger.warning("Password reset refresh-session cleanup failed")

    def _reset_password_sync(
        self, user_id: int, version: int, new_password: str
    ) -> None:
        try:
            current_version = self.db.scalar(
                select(User.token_version).where(User.id == user_id)
            )
            if current_version != version:
                raise BusinessError(INVALID_RESET_MESSAGE)
            password_hash = hash_password(new_password)
            # Conditional UPDATE serializes concurrent resets with different
            # tokens for the same account. Only one token at this version wins.
            result = self.db.execute(
                update(User)
                .where(User.id == user_id, User.token_version == version)
                .values(password_hash=password_hash, token_version=User.token_version + 1)
                .returning(User.id)
                .execution_options(synchronize_session=False)
            )
            if result.scalar_one_or_none() is None:
                raise BusinessError(INVALID_RESET_MESSAGE)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
