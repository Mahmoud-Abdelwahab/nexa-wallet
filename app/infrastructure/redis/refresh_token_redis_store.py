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