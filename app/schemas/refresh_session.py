from datetime import datetime

from pydantic import BaseModel


class RefreshSession(BaseModel):
    session_id: str
    user_id: int
    absolute_expires_at: datetime