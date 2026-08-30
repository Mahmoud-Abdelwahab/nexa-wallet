import json

from redis.asyncio import Redis
from app.core.config import settings
from app.schemas.refresh_session import RefreshSession


class RefreshTokenStore:

    def __init__(self, redis_client: Redis):
        self.redis = redis_client

    async def store_session(
        self,
        token_hash: str,
        session: RefreshSession,
    ) -> None:

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

        key = f"refresh_session:{token_hash}"

        value = await self.redis.get(key)

        if value is None:
            return None

        return RefreshSession.model_validate_json(value)
    
    async def revoke_session(
            self,
            token_hash: str,
        ) -> None:
    
            key = f"refresh_session:{token_hash}"
    
            await self.redis.delete(key)
    
    async def rotate_session(
        self,
        old_token_hash: str,
        new_token_hash: str,
        session: RefreshSession,
        ttl_seconds: int,
    ) -> bool:

        old_key = f"refresh_session:{old_token_hash}"
        new_key = f"refresh_session:{new_token_hash}"

        result = await self.redis.eval(
            ROTATE_SESSION_SCRIPT,
            2,
            old_key,
            new_key,
            session.model_dump_json(),
            ttl_seconds,
        )

        return result == 1


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
