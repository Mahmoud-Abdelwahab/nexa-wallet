import json

from redis.asyncio import Redis
from app.core.config import settings
from app.schemas.refresh_session import RefreshSession


class RefreshTokenRedisStore:

    def __init__(self, redis_client: Redis):
        """Initialize the store with the shared async Redis client."""
        self.redis = redis_client

#! refresh_session:{token_hash}
# →  To get specific Session durectly using the Refresh Token
# user_refresh_sessions:{user_id}
# to get all session easly for the same user to log him out from all devices -Logout All Devices

# Redis structure:
#
#                         User 1
#                           |
#             user_refresh_sessions:1  (SET)
#                           |
#                     {AAA, BBB}
#                       /     \
#                      /       \
#                     v         v
#     refresh_session:AAA    refresh_session:BBB
#          (KEY -> VALUE)         (KEY -> VALUE)
#          AAA -> Session         BBB -> Session
#
# Each refresh session has its own key:
#   refresh_session:{token_hash} -> session data
#
# Each user also has one Set containing the hashes of all their sessions:
#   user_refresh_sessions:{user_id} -> {token_hash_1, token_hash_2, ...}

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

        #* used to clean up any stale session hashes from the user's index before storing a new session. This ensures that the user's session index remains accurate and does not contain references to sessions that have already expired or been revoked.
        await self.cleanup_stale_sessions(session.user_id) 

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
            pipe.set(  # set USED TO store the session data in Redis with a TTL (time-to-live) that represents the idle expiration time for the refresh token. This means that if the refresh token is not used within this time frame, it will automatically expire and be removed from Redis.
                session_key,
                value,
                ex=ttl_seconds,
            )
            pipe.sadd(  # sadd USED TO add the token hash to the user's session index in a set to be able to get all sessions for the same user to log him out from all devices
                # user_refresh_sessions:{user_id} ==> {AAA, BBB} AAA is the token hash for the session
                user_sessions_key,
                token_hash,
            )

            await pipe.execute()

    async def cleanup_stale_sessions(self, user_id: int) -> None:
        """Remove hashes whose individual refresh-session keys have expired."""

        user_sessions_key = f"user_refresh_sessions:{user_id}"
        token_hashes = list(await self.redis.smembers(user_sessions_key))

        if not token_hashes:
            return

        async with self.redis.pipeline(transaction=True) as pipe:
            for token_hash in token_hashes:
                pipe.exists(f"refresh_session:{token_hash}")

            exists_results = await pipe.execute()

        stale_hashes = [
            token_hash
            for token_hash, exists in zip(token_hashes, exists_results)
            if not exists
        ]

        if stale_hashes:
            await self.redis.srem(user_sessions_key, *stale_hashes)

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
            pipe.srem(  # srem USED TO remove the token hash from the user's session index in a set
                user_sessions_key,
                token_hash,
            )

            await pipe.execute()

    async def revoke_all_sessions(self, user_id: int) -> None:
        """Atomically revoke every refresh session belonging to a user."""

        user_sessions_key = f"user_refresh_sessions:{user_id}"

        # The Lua script takes the Set snapshot and deletes its session keys
        # in one atomic operation, so rotation cannot create an orphaned key
        # between SMEMBERS and DEL.
        await self.redis.eval(
            REVOKE_ALL_SESSIONS_SCRIPT,
            1,
            user_sessions_key,
        )

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
        user_sessions_key = f"user_refresh_sessions:{session.user_id}"
        # eval(script, numkeys, key1, key2, ..., arg1, arg2, ...)
        result = await self.redis.eval(
            ROTATE_SESSION_SCRIPT,
            # * numkeys --> 3: old key, new key, and the user's session Set.
            3,
            old_key,
            new_key,
            user_sessions_key,
            session.model_dump_json(),
            ttl_seconds,
            old_token_hash,
            new_token_hash,
        )

        return result == 1


# KEYS[1]	old_key = refresh_session:{old_token_hash}	المفتاح اللي عايزين نتأكد إنه موجود ونمسحه
# KEYS[2]	new_key = refresh_session:{new_token_hash}	المفتاح الجديد اللي هننشئه
# KEYS[3]	user_sessions_key = user_refresh_sessions:{user_id}	Set التي تحتوي hashes جلسات المستخدم
# ARGV[1]	session.model_dump_json()	القيمة اللي هتتخزن — بيانات السيشن (session_id, user_id, absolute_expires_at)
# ARGV[2]	ttl_seconds	مدة صلاحية المفتاح الجديد (TTL) حسب الوقت المتبقي حتى absolute expiry
# ARGV[3]	old_token_hash	الهاش القديم الذي سيتم حذفه من Set المستخدم
# ARGV[4]	new_token_hash	الهاش الجديد الذي سيتم إضافته إلى Set المستخدم

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
redis.call("SREM", KEYS[3], ARGV[3])

redis.call(
    "SET",
    KEYS[2],
    ARGV[1],
    "EX",
    ARGV[2]
)
redis.call("SADD", KEYS[3], ARGV[4])

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

# Logout-all is atomic with refresh-token rotation because both operations are
# Redis Lua scripts executed by Redis one at a time.
REVOKE_ALL_SESSIONS_SCRIPT = """
local token_hashes = redis.call("SMEMBERS", KEYS[1])

for _, token_hash in ipairs(token_hashes) do
    redis.call("DEL", "refresh_session:" .. token_hash)
end

redis.call("DEL", KEYS[1])

return #token_hashes
"""
