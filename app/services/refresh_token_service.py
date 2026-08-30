from datetime import datetime, timedelta, timezone

import secrets
import hashlib

from app.schemas.refresh_session import RefreshSession
from app.core.config import settings

# it's responsability is :-
# raw token
#    ↓
# hash
#    ↓
# session

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.schemas.refresh_session import RefreshSession


class RefreshTokenService:

    def generate_refresh_token(self) -> str:
        return secrets.token_urlsafe(32)# 32 bytes = 43 characters, Return a random URL-safe text string, in Base64 encoding.

    def hash_refresh_token(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()  # Return the SHA-256 hash of the token as a hexadecimal string. This is used to store the token securely in Redis without exposing the actual token value.

    def create_refresh_session(
        self,
        user_id: int,
    ) -> tuple[str, str, RefreshSession]:

        refresh_token = self.generate_refresh_token()

        now = datetime.now(timezone.utc)

        session = RefreshSession(
            session_id=secrets.token_urlsafe(16),
            user_id=user_id,
            absolute_expires_at=now + timedelta(
                days=settings.REFRESH_TOKEN_ABSOLUTE_DAYS
            ),
        )

        refresh_token_hash = self.hash_refresh_token(refresh_token)

        return refresh_token, refresh_token_hash, session

    
    #                 create_refresh_session()
    #                           │
    #          ┌────────────────┼────────────────┐
    #          ▼                ▼                ▼
    #    raw token     refresh token hash     session
    #          │                │                │
    #          │                │                ├── session_id
    #          │                │                ├── user_id
    #          │                │                └── absolute expiry
    #          │                │
    #          │                ▼
    #          │              Redis
    #          │
    #          ▼
    #     API response

    ## TTL ( Time To Live ) for resfresh session in Redis is set to 30 days (REFRESH_IDLE_DAYS) to support idle expiration. The absolute expiration is stored in the session object and is set to 90 days (REFRESH_ABSOLUTE_DAYS).
    # We will set TTL for the refresh session in Redis to 30 days (REFRESH_IDLE_DAYS) to support idle expiration. The absolute expiration is stored in the session object and is set to 90 days (REFRESH_ABSOLUTE_DAYS).