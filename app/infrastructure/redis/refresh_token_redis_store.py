import json

from redis.asyncio import Redis
from app.core.config import settings
from app.schemas.refresh_session import RefreshSession


class RefreshTokenStore:

    def __init__(self, redis_client: Redis):
        """Initialize the store with the shared async Redis client."""
        self.redis = redis_client

#! refresh_session:{token_hash}
# →  To get specific Session durectly using the Refresh Token
# user_refresh_sessions:{user_id}
# to get all session easly for the same user to log him out from all devices -Logout All Devices

# Redis data model:
#
# 1) SET stores the complete session data:
#    refresh_session:AAA -> JSON(session_id, user_id, absolute_expires_at)
#    The EX option adds a TTL, so Redis removes this session automatically.
#
# 2) SADD stores only the token hash in the user's index:
#    user_refresh_sessions:1 -> {AAA, BBB}
#    A Redis Set keeps each hash unique and helps find all sessions for a user.
#
# Both keys have different jobs:
# - refresh_session:{token_hash}: fast lookup of one specific session.
# - user_refresh_sessions:{user_id}: index of a user's sessions for logout-all.
#
# The pipeline groups SET and SADD into one Redis transaction. This reduces
# network round-trips and prevents another Redis command from running between
# the two writes, keeping the session data and its user index synchronized.

    async def store_session(
        self,
        token_hash: str,
        session: RefreshSession,
    ) -> None:
        """Save a refresh session in Redis using the configured idle TTL."""

        # Login flow:
        # 1. SET the session JSON with its idle TTL.
        # 2. SADD the token hash to the user's session index.
        # Example: SET refresh_session:AAA + SADD user_refresh_sessions:1 AAA.

        session_key = f"refresh_session:{token_hash}"
        user_sessions_key = (
                f"user_refresh_sessions:{session.user_id}"
            )

        ttl_seconds = (
                settings.REFRESH_TOKEN_IDLE_DAYS
                * 24
                * 60
                * 60
            )

        value = session.model_dump_json()

        async with self.redis.pipeline(transaction=True) as pipe:
                pipe.set(
                    session_key,
                    value,
                    ex=ttl_seconds,
                )
                pipe.sadd(
                    user_sessions_key,
                    token_hash,
                )

                await pipe.execute()

    async def get_session(
        self,
        token_hash: str,
    ) -> RefreshSession | None:
        """Load and deserialize a refresh session, or return None if absent."""

        # Refresh flow: GET refresh_session:{token_hash} to find one session.

        key = f"refresh_session:{token_hash}"

        value = await self.redis.get(key)

        if value is None:
            return None

        return RefreshSession.model_validate_json(value)

    async def revoke_session(
        self,
        token_hash: str,
        user_id: int,
    ) -> None:
        """Delete a session and remove its hash from the user's index."""

        # Logout flow: delete the session key and SREM its hash from the index.

        session_key = f"refresh_session:{token_hash}"
        user_sessions_key = (
            f"user_refresh_sessions:{user_id}"
        )

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(session_key)
            pipe.srem(
                user_sessions_key,
                token_hash,
            )

            await pipe.execute()

    # A exists → DELETE A → CREATE B → SUCCESS
    async def rotate_session(
        self,
        old_token_hash: str,
        new_token_hash: str,
        session: RefreshSession,
        ttl_seconds: int,
    ) -> bool:
        """Atomically replace the old token with a new token in Redis."""

        # Refresh-token rotation:
        # 1. Check that the old key exists.
        # 2. Delete the old key so it cannot be reused.
        # 3. Create the new key with the new token and remaining TTL.
        # Lua executes these steps atomically, preventing concurrent reuse.

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

# Example of the complete flow:
#
# Login:
#   SET  refresh_session:AAA       -> session JSON + TTL
#   SADD user_refresh_sessions:1   -> AAA
#
# Refresh:
#   GET  refresh_session:AAA       -> read the old session
#   Lua: DEL refresh_session:AAA, then SET refresh_session:BBB -> new session
#
# Logout all devices:
#   SMEMBERS user_refresh_sessions:1 -> [AAA, BBB]
#   DEL refresh_session:AAA and refresh_session:BBB
#   DEL user_refresh_sessions:1

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
