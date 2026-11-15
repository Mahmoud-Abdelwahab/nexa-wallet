from redis.asyncio import Redis


RECIPIENT_MINUTE_LIMIT = 5
MINUTE_SECONDS = 60
DAY_SECONDS = 24 * 60 * 60

# Both counters and their expirations are updated atomically, including when
# multiple lookup requests arrive for the same user at the same time.
RECIPIENT_RATE_LIMIT_SCRIPT = """
local minute_count = redis.call('INCR', KEYS[1])
if minute_count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local day_count = redis.call('INCR', KEYS[2])
if day_count == 1 then redis.call('EXPIRE', KEYS[2], ARGV[2]) end
if minute_count > tonumber(ARGV[3]) or day_count > tonumber(ARGV[4]) then
    return 0
end
return 1
"""


async def allow_recipient_lookup(redis: Redis, user_id: int, daily_limit: int) -> bool:
    result = await redis.eval(
        RECIPIENT_RATE_LIMIT_SCRIPT,
        2,
        f"recipient_lookup_rate:{user_id}:minute",
        f"recipient_lookup_rate:{user_id}:day",
        MINUTE_SECONDS,
        DAY_SECONDS,
        RECIPIENT_MINUTE_LIMIT,
        daily_limit,
    )
    return result == 1
