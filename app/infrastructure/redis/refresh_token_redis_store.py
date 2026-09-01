import json

from redis.asyncio import Redis
from app.core.config import settings
from app.schemas.refresh_session import RefreshSession


class RefreshTokenStore:

    def __init__(self, redis_client: Redis):
        """Initialize the store with the shared async Redis client."""
        self.redis = redis_client

    async def store_session(
        self,
        token_hash: str,
        session: RefreshSession,
    ) -> None:
        """Save a refresh session in Redis using the configured idle TTL."""

        key = f"refresh_session:{token_hash}"

        value = session.model_dump_json()

        ttl_seconds = (
            settings.REFRESH_TOKEN_IDLE_DAYS
            * 24
            * 60
            * 60
        )

        await self.redis.set(
            key,
            value,
            ex=ttl_seconds,
        )

    async def get_session(
        self,
        token_hash: str,
    ) -> RefreshSession | None:
        """Load and deserialize a refresh session, or return None if absent."""

        key = f"refresh_session:{token_hash}"

        value = await self.redis.get(key)

        if value is None:
            return None

        return RefreshSession.model_validate_json(value)

    async def revoke_session(
        self,
        token_hash: str,
    ) -> None:
        """Delete a refresh session from Redis to revoke its token."""

        key = f"refresh_session:{token_hash}"

        await self.redis.delete(key)

    # A exists → DELETE A → CREATE B → SUCCESS
    async def rotate_session(
        self,
        old_token_hash: str,
        new_token_hash: str,
        session: RefreshSession,
        ttl_seconds: int,
    ) -> bool:
        """Atomically replace the old token with a new token in Redis."""

        old_key = f"refresh_session:{old_token_hash}"
        new_key = f"refresh_session:{new_token_hash}"
        # eval(script, numkeys, key1, key2, ..., arg1, arg2, ...)
        result = await self.redis.eval(
            ROTATE_SESSION_SCRIPT,
            # * this  numkeys --> 2 is the number of keys here we have two keys [ old_key, new_key] after these two you can find the argument values which are model_dump_json and ttl_seconds
            2,
            old_key,
            new_key,
            session.model_dump_json(),
            ttl_seconds,
        )

        return result == 1


# KEYS[1]	old_key = refresh_session:{old_token_hash}	المفتاح اللي عايزين نتأكد إنه موجود ونمسحه
# KEYS[2]	new_key = refresh_session:{new_token_hash}	المفتاح الجديد اللي هننشئه
# ARGV[1]	session.model_dump_json()	القيمة اللي هتتخزن — بيانات السيشن (session_id, user_id, absolute_expires_at)
# ARGV[2]	ttl_seconds	مدة صلاحية المفتاح الجديد (TTL) considering the last period

# This script confirm atomicity and prevent the race condition
ROTATE_SESSION_SCRIPT = """
local old_value = redis.call("GET", KEYS[1])

if not old_value then
    return 0
end

redis.call("DEL", KEYS[1])

redis.call(
    "SET",
    KEYS[2],
    ARGV[1],
    "EX",
    ARGV[2]
)

return 1
"""
# Redis
# KEY
# refresh_session:abc...

# VALUE
# {
#    session_id,
#    user_id,
#    absolute_expires_at
# }

# TTL
# 30 days
