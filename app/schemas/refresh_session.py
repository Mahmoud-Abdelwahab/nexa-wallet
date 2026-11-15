from datetime import datetime

from app.schemas.device_session import DeviceMetadata


class RefreshSession(DeviceMetadata):
    # Inherit device_name and platform into the existing Redis refresh-session record.
    # Each login gets a new session_id; each refresh keeps that ID and replaces the token.
    session_id: str
    user_id: int
    # Legacy sessions were issued when token_version was zero. After any
    # credential change, they must fail the same check as versioned sessions.
    token_version: int = 0
    # Absolute expiration is fixed; rotation must not restart the session lifetime.
    absolute_expires_at: datetime
    # created_at stays fixed from login; last_seen_at updates only on refresh.
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
