# Redis key map (token_hash is not the raw token):
#
# user_refresh_sessions:7  -> {AAA, BBB}         Index of the user's refresh hashes
# refresh_session:AAA     -> JSON(sid=S1, ...)  Record for one login session
# auth_session:7:S1       -> AAA               Pointer to the current hash for sid S1
#
# Login:    S1 -> AAA
# Rotation: S1 -> BBB; delete AAA while preserving sid.
# Revoke: remove S1, record BBB and its index membership; subsequent authentication returns 401.
#
# The pointer lets a JWT sid locate its session without knowing the raw refresh token.
# The Lua scripts make authentication reads, revocation and rotation atomic relative to one another.
from datetime import datetime, timezone

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
# The third key, auth_session:user:sid, resolves the current hash after each rotation.
# The pipeline stores the record, user index and pointer together, not just the first two keys.
# Each key has a different job:
# - refresh_session:{token_hash}: fast lookup of one specific session.
# - user_refresh_sessions:{user_id}: index of a user's sessions for logout-all.
#
# The pipeline groups the record SET, index SADD and pointer SET in one transaction. This reduces
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

        # The pointer must not outlive the session: use the shorter idle or remaining absolute TTL.
        ttl_seconds = min(
            settings.REFRESH_TOKEN_IDLE_DAYS * 24 * 60 * 60,
            int((session.absolute_expires_at - datetime.now(timezone.utc)).total_seconds()),
        )
        if ttl_seconds <= 0:
            raise ValueError("Cannot store an expired refresh session")

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

            # Give the record and pointer matching TTLs; a sid pointer cannot replace a live refresh record.
            pipe.set(
                active_session_key(session.user_id, session.session_id),
                token_hash,
                ex=ttl_seconds,
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

        await self.redis.eval(
            REVOKE_TOKEN_SCRIPT, 2,
            f"refresh_session:{token_hash}", f"user_refresh_sessions:{user_id}",
            str(user_id), token_hash,
        )

    async def revoke_session_id(self, user_id: int, session_id: str) -> bool:
        # KEYS[1]: user index; ARGV[1]: owner ID; ARGV[2]: session ID to revoke.
        # Zero means no matching record; a positive result means at least one matching record was revoked.
        result = await self.redis.eval(
            REVOKE_SESSION_SCRIPT, 1, f"user_refresh_sessions:{user_id}",
            str(user_id), session_id,
        )
        return result > 0

    async def get_active_session(self, user_id: int, session_id: str) -> RefreshSession | None:
        # Why not GET the pointer and then GET the record in two separate round trips?
        # Rotation could delete the old hash between those reads, causing a false 401.
        # Lua reads both atomically, seeing either the state before rotation or the state after it.
        # Read the pointer and its record atomically with rotation/revocation.
        value = await self.redis.eval(
            ACTIVE_SESSION_SCRIPT, 1, active_session_key(user_id, session_id),
            str(user_id), session_id,
        )
        if not value:
            return None
        session = RefreshSession.model_validate_json(value)
        # Check absolute expiration too, even if a TTL was misconfigured or a record unexpectedly remains.
        if session.absolute_expires_at <= datetime.now(timezone.utc):
            return None
        return session

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
            # Refresh record, user index and stable sid pointer change together.
            4,
            old_key,
            new_key,
            user_sessions_key,
            active_session_key(session.user_id, session.session_id),
            session.model_dump_json(),
            ttl_seconds,
            old_token_hash,
            new_token_hash,
        )

        return result == 1


# KEYS[1]: old_key = refresh_session:{old_token_hash}; the record to verify and consume.
# KEYS[2]: new_key = refresh_session:{new_token_hash}; the replacement record.
# KEYS[3]: user_refresh_sessions:{user_id}; the set of the user's refresh hashes.
# KEYS[4]: auth_session:{user_id}:{sid}; the stable pointer to the current refresh hash.
# ARGV[1]: session.model_dump_json(); the replacement session data.
# ARGV[2]: ttl_seconds; expiration capped by the remaining absolute session lifetime.
# ARGV[3]: old_token_hash; remove it from the user index.
# ARGV[4]: new_token_hash; add it to the user index.

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
-- The old record and its matching pointer must still exist; prior revocation or rotation blocks reuse.
local old_value = redis.call("GET", KEYS[1])

if not old_value or redis.call("GET", KEYS[4]) ~= ARGV[3] then
    return 0
end

-- Remove the old record and index membership before storing the replacement.
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
-- Point the same sid to the new hash with its TTL; refresh must not create a new session identity.
redis.call("SET", KEYS[4], ARGV[4], "EX", ARGV[2])

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
-- Remove each sid pointer and refresh record, then delete the user index.
local token_hashes = redis.call("SMEMBERS", KEYS[1])

for _, token_hash in ipairs(token_hashes) do
    local key = "refresh_session:" .. token_hash
    local raw = redis.call("GET", key)
    if raw then
        local session = cjson.decode(raw)
        redis.call("DEL", "auth_session:" .. tostring(session.user_id) .. ":" .. session.session_id)
    end
    redis.call("DEL", key)
end

redis.call("DEL", KEYS[1])

return #token_hashes
"""



def active_session_key(user_id: int, session_id: str) -> str:
    # Namespace by user ID and sid so identical session IDs cannot mix records across accounts.
    return f"auth_session:{user_id}:{session_id}"


ACTIVE_SESSION_SCRIPT = """
-- sid pointer -> current refresh hash -> session record; reject any missing link.
local token_hash = redis.call("GET", KEYS[1])
if not token_hash then return false end
local raw = redis.call("GET", "refresh_session:" .. token_hash)
if not raw then return false end
local session = cjson.decode(raw)
-- The record must match both the user and sid requested by the signed JWT.
if tostring(session.user_id) ~= ARGV[1] or session.session_id ~= ARGV[2] then
    return false
end
return raw
"""

REVOKE_TOKEN_SCRIPT = """
-- Hash-based revocation: verify record ownership before deleting anything.
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local session = cjson.decode(raw)
if tostring(session.user_id) ~= ARGV[1] then return 0 end
local active_key = "auth_session:" .. ARGV[1] .. ":" .. session.session_id
-- Do not delete a pointer already moved to another hash; an obsolete token cannot revoke its replacement.
if redis.call("GET", active_key) == ARGV[2] then redis.call("DEL", active_key) end
redis.call("DEL", KEYS[1])
redis.call("SREM", KEYS[2], ARGV[2])
return 1
"""

# Shares Redis atomicity with ROTATE_SESSION_SCRIPT. No rotated token can
# survive deletion of its sid, regardless of which operation runs first.
REVOKE_SESSION_SCRIPT = """
-- Inspect only the requesting user's index and delete records matching both the owner and requested sid.
local count = 0
local hashes = redis.call("SMEMBERS", KEYS[1])
for _, token_hash in ipairs(hashes) do
    local key = "refresh_session:" .. token_hash
    local raw = redis.call("GET", key)
    if not raw then
        redis.call("SREM", KEYS[1], token_hash)
    else
        local session = cjson.decode(raw)
        if tostring(session.user_id) == ARGV[1] and session.session_id == ARGV[2] then
            redis.call("DEL", key)
            redis.call("SREM", KEYS[1], token_hash)
            count = count + 1
        end
    end
end
-- Deleting the pointer revokes access JWTs as well as refresh tokens.
-- If rotation ran first, delete its new record; if deletion ran first, rotation fails its preconditions.
redis.call("DEL", "auth_session:" .. ARGV[1] .. ":" .. ARGV[2])
return count
"""
