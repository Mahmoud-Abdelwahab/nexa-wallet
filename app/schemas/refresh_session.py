from datetime import datetime

from app.schemas.device_session import DeviceMetadata


class RefreshSession(DeviceMetadata):
    # الوراثة بتضيف device_name وplatform لنفس سجل الـ Refresh الموجود في Redis.
    # كل Login جديد: session_id جديدة. كل Refresh: نفس session_id وتوكن مختلفة.
    session_id: str
    user_id: int
    # Legacy sessions were issued when token_version was zero. After any
    # credential change, they must fail the same check as versioned sessions.
    token_version: int = 0
    # النهاية القصوى للجلسة ثابتة؛ الـ Rotation ما تبدأش عمر الجلسة من الأول.
    absolute_expires_at: datetime
    # created_at ثابتة من أول Login، وlast_seen_at بتتحدّث عند الـ Refresh فقط.
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
