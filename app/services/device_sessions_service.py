# Device list: user index -> refresh records -> active-session filters -> display fields only.
# This service organizes display data; RefreshTokenRedisStore performs atomic revocation.
from datetime import datetime, timezone

from redis.asyncio import Redis

from app.infrastructure.redis.refresh_token_redis_store import (
    RefreshTokenRedisStore,
    active_session_key,
)
from app.schemas.device_session import DeviceSessionResponse
from app.schemas.refresh_session import RefreshSession


class DeviceSessionsService:
    def __init__(self, redis: Redis):
        self.redis = redis
        self.store = RefreshTokenRedisStore(redis)

    async def list_sessions(
        self, user_id: int, token_version: int, current_session_id: str
    ) -> list[DeviceSessionResponse]:
        # Redis expires records automatically, but their hashes may remain in the set; remove stale entries.
        await self.store.cleanup_stale_sessions(user_id)
        hashes = list(await self.redis.smembers(f"user_refresh_sessions:{user_id}"))
        if not hashes:
            return []
        # Support both bytes and str responses; MGET fetches all records in one round trip.
        hashes = [h.decode() if isinstance(h, bytes) else h for h in hashes]
        records = await self.redis.mget([f"refresh_session:{h}" for h in hashes])
        candidates = []
        now = datetime.now(timezone.utc)
        for token_hash, raw in zip(hashes, records):
            if not raw:
                continue
            session = RefreshSession.model_validate_json(raw)
            # Check ownership, account version and absolute expiration even when an old record still exists.
            if (session.user_id == user_id and session.token_version == token_version
                    and session.absolute_expires_at > now):
                candidates.append((token_hash, session))
        if not candidates:
            return []
        # A refresh record alone is insufficient; its sid must point to the current hash.
        # Pre-sid records and revoked sessions must not appear as active devices.
        pointers = await self.redis.mget([
            active_session_key(user_id, session.session_id) for _, session in candidates
        ])
        # Keying the result by sid prevents duplicate rows for the same login session.
        # This list is a snapshot: concurrent rotation may briefly hide a row but cannot authorize a revoked session.
        sessions = {}
        for (token_hash, session), pointer in zip(candidates, pointers):
            if isinstance(pointer, bytes):
                pointer = pointer.decode()
            if pointer != token_hash:
                continue
            sessions[session.session_id] = DeviceSessionResponse(
                session_id=session.session_id,
                # sid survives rotation, so current-session identification remains correct after refresh.
                is_current=session.session_id == current_session_id,
                device_name=session.device_name,
                platform=session.platform,
                created_at=session.created_at,
                last_seen_at=session.last_seen_at,
            )
        return list(sessions.values())

    async def revoke_session(self, user_id: int, session_id: str) -> bool:
        # Use the authenticated user ID so a caller cannot revoke another account's session.
        # Lua removes the device record, index membership and sid pointer atomically.
        return await self.store.revoke_session_id(user_id, session_id)
