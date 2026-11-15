# شاشة الأجهزة: Index المستخدم -> سجلات Refresh -> فلاتر النشاط -> بيانات عرض فقط.
# الخدمة دي بتنظّم البيانات؛ تنفيذ الحذف الذرّي مسؤولية RefreshTokenRedisStore.
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
        # Redis بيمسح السجل المنتهي تلقائيًا، لكن الهاش ممكن يفضل في الـ Set؛ ننضّفه.
        await self.store.cleanup_stale_sessions(user_id)
        hashes = list(await self.redis.smembers(f"user_refresh_sessions:{user_id}"))
        if not hashes:
            return []
        # ندعم Redis سواء بيرجع bytes أو str، وMGET تجيب السجلات في طلب واحد.
        hashes = [h.decode() if isinstance(h, bytes) else h for h in hashes]
        records = await self.redis.mget([f"refresh_session:{h}" for h in hashes])
        candidates = []
        now = datetime.now(timezone.utc)
        for token_hash, raw in zip(hashes, records):
            if not raw:
                continue
            session = RefreshSession.model_validate_json(raw)
            # نتأكد من الملكية والنسخة والنهاية القصوى، حتى لو سجل قديم لسه موجود.
            if (session.user_id == user_id and session.token_version == token_version
                    and session.absolute_expires_at > now):
                candidates.append((token_hash, session))
        if not candidates:
            return []
        # وجود سجل Refresh وحده مش كفاية؛ لازم sid تشير للهاش الحالي فعلًا.
        # سجل من قبل تحديث sid أو سجل اتلغى ما يظهرش كجهاز Active.
        pointers = await self.redis.mget([
            active_session_key(user_id, session.session_id) for _, session in candidates
        ])
        # Dictionary بالمفتاح sid تمنع تكرار نفس الجلسة في نتيجة العرض.
        # القائمة snapshot؛ Rotation متزامنة قد تخفي عنصر مؤقتًا، مش تسمح بدخول ملغي.
        sessions = {}
        for (token_hash, session), pointer in zip(candidates, pointers):
            if isinstance(pointer, bytes):
                pointer = pointer.decode()
            if pointer != token_hash:
                continue
            sessions[session.session_id] = DeviceSessionResponse(
                session_id=session.session_id,
                # sid ثابتة خلال Rotation، فتمييز الجهاز الحالي يفضل صحيح بعد التجديد.
                is_current=session.session_id == current_session_id,
                device_name=session.device_name,
                platform=session.platform,
                created_at=session.created_at,
                last_seen_at=session.last_seen_at,
            )
        return list(sessions.values())

    async def revoke_session(self, user_id: int, session_id: str) -> bool:
        # نمرّر user_id من المستخدم الموثّق، عشان ما نقدرش نحذف جلسة حساب تاني.
        # Lua تمسح سجل الجهاز والهاش من الـ Index ومؤشر sid كعملية واحدة.
        return await self.store.revoke_session_id(user_id, session_id)
