import hashlib
import secrets

from redis.asyncio import Redis


RESET_TTL_SECONDS = 15 * 60


def reset_key(token: str) -> str:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"password_reset:{token_hash}"


async def create_reset_token(redis: Redis, user_id: int, token_version: int) -> str:
    token = secrets.token_urlsafe(32)
    await redis.set(
        reset_key(token), f"{user_id}:{token_version}", ex=RESET_TTL_SECONDS
    )
    return token


async def consume_reset_token(redis: Redis, token: str) -> tuple[int, int] | None:
    # GETDEL is atomic: concurrent requests cannot consume the same token.
    value = await redis.getdel(reset_key(token))
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    user_id, version = value.split(":")
    return int(user_id), int(version)
