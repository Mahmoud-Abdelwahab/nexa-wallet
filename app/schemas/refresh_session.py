from datetime import datetime

from pydantic import BaseModel


class RefreshSession(BaseModel):
    session_id: str
    user_id: int
    # Legacy sessions were issued when token_version was zero. After any
    # credential change, they must fail the same check as versioned sessions.
    token_version: int = 0
    absolute_expires_at: datetime
