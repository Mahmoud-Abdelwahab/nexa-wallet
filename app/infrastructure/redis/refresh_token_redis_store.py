# خريطة Redis الجديدة (الـ token_hash مش التوكن الخام):
#
# user_refresh_sessions:7  -> {AAA, BBB}        فهرس كل جلسات المستخدم
# refresh_session:AAA     -> JSON(sid=S1, ...) سجل جلسة جهاز واحد
# auth_session:7:S1       -> AAA              مؤشر للهاش الحالي لنفس sid
#
# Login:    S1 -> AAA
# Rotation: S1 -> BBB، وAAA تتحذف، وsid نفسها ما تتغيّرش
# Revoke:   نحذف S1 وسجل BBB وعضويته في الفهرس؛ التحقق التالي يرجع 401
#
# وجود المؤشر يسمح بالتحقق من JWT فيها sid من غير ما نعرف Refresh token الخام.
# الـ Lua scripts تحت تجعل التحقق/الحذف/Rotation ذرّية بالنسبة لبعض داخل Redis.
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
# المفتاح الثالث auth_session:user:sid يوصلنا للهاش الحالي بعد كل Rotation.
# الـ pipeline بتحفظ السجل والفهرس والمؤشر مع بعض، مش أول مفتاحين بس.
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

        # ما نخليش المؤشر عايش أطول من الجلسة: الأقصر بين idle TTL والنهاية القصوى.
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

            # نفس TTL لسجل Refresh والمؤشر؛ وجود sid وحدها مش بديل للسجل الحي.
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
        # KEYS[1] فهرس المستخدم، ARGV[1] صاحبه، ARGV[2] الجلسة المطلوب حذفها.
        # 0 معناها مفيش سجل مطابق (مفقودة/محذوفة/مش ملكه)، وأكبر من صفر نجاح الحذف.
        result = await self.redis.eval(
            REVOKE_SESSION_SCRIPT, 1, f"user_refresh_sessions:{user_id}",
            str(user_id), session_id,
        )
        return result > 0

    async def get_active_session(self, user_id: int, session_id: str) -> RefreshSession | None:
        # ليه مش GET للمؤشر وبعدين GET للسجل في طلبين؟
        # ممكن Rotation تدخل بينهم فتحذف الهاش القديم؛ ده كان هيعمل 401 غلط.
        # Lua تقرأ الاثنين كخطوة واحدة، فتشوف يا الحالة قبل Rotation يا بعدها.
        # Read the pointer and its record atomically with rotation/revocation.
        value = await self.redis.eval(
            ACTIVE_SESSION_SCRIPT, 1, active_session_key(user_id, session_id),
            str(user_id), session_id,
        )
        if not value:
            return None
        session = RefreshSession.model_validate_json(value)
        # فحص إضافي للنهاية القصوى، حتى لو TTL اتضبطت غلط أو السجل اتبقى لأي سبب.
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


# KEYS[1]	old_key = refresh_session:{old_token_hash}	المفتاح اللي عايزين نتأكد إنه موجود ونمسحه
# KEYS[2]	new_key = refresh_session:{new_token_hash}	المفتاح الجديد اللي هننشئه
# KEYS[3]	user_sessions_key = user_refresh_sessions:{user_id}	Set التي تحتوي hashes جلسات المستخدم
# KEYS[4] auth_session:{user_id}:{sid} مؤشر sid الثابت، وقيمته هاش Refresh الحالية.
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
-- AAA لازم تكون لسه موجودة والمؤشر لسه بيشير لها؛ الحذف أو Rotation سابقة تمنع التجديد.
local old_value = redis.call("GET", KEYS[1])

if not old_value or redis.call("GET", KEYS[4]) ~= ARGV[3] then
    return 0
end

-- امسح الهاش القديمة من السجل والفهرس قبل حفظ البديلة.
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
-- نفس sid تشير للهاش الجديدة بنفس TTL؛ ما نعملش هوية جهاز جديدة مع كل Refresh.
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
-- لكل جهاز: امسح مؤشر sid والسجل، وبعدها احذف فهرس المستخدم كله.
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
    # ربط المفتاح بالمستخدم وsid يمنع خلط نفس اسم الجلسة بين حسابين.
    return f"auth_session:{user_id}:{session_id}"


ACTIVE_SESSION_SCRIPT = """
-- مؤشر sid -> هاش Refresh الحالية -> سجل الجلسة؛ نرفض لو أي حلقة مفقودة.
local token_hash = redis.call("GET", KEYS[1])
if not token_hash then return false end
local raw = redis.call("GET", "refresh_session:" .. token_hash)
if not raw then return false end
local session = cjson.decode(raw)
-- لازم السجل نفسه يخص المستخدم وsid اللي الـ JWT الموقعة بتطلبهم.
if tostring(session.user_id) ~= ARGV[1] or session.session_id ~= ARGV[2] then
    return false
end
return raw
"""

REVOKE_TOKEN_SCRIPT = """
-- مسار الإلغاء بالهاش: تحقق من صاحب السجل قبل أي حذف.
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local session = cjson.decode(raw)
if tostring(session.user_id) ~= ARGV[1] then return 0 end
local active_key = "auth_session:" .. ARGV[1] .. ":" .. session.session_id
-- ما نمسحش مؤشر اتحدّث لهاش مختلفة؛ التوكن القديمة مش تصريح بإلغاء البديلة.
if redis.call("GET", active_key) == ARGV[2] then redis.call("DEL", active_key) end
redis.call("DEL", KEYS[1])
redis.call("SREM", KEYS[2], ARGV[2])
return 1
"""

# Shares Redis atomicity with ROTATE_SESSION_SCRIPT. No rotated token can
# survive deletion of its sid, regardless of which operation runs first.
REVOKE_SESSION_SCRIPT = """
-- نفحص فهرس صاحب الطلب فقط ونحذف السجلات المطابقة له ولـ sid المطلوبة.
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
-- حذف المؤشر يقفل وصول الـ Access JWT كمان، مش Refresh token بس.
-- لو Rotation سبقتنا هنلاقي الهاش الجديدة ونمسحها؛ لو سبَقناها هتفشل شروطها.
redis.call("DEL", "auth_session:" .. ARGV[1] .. ":" .. ARGV[2])
return count
"""
