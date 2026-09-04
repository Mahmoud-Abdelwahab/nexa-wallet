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
# *  Refresh Token one-time use

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.schemas.refresh_session import RefreshSession


class RefreshTokenService:

    def generate_refresh_token(self) -> str:
        """Generate a secure random refresh token for the client."""
        return secrets.token_urlsafe(32)  # 32 bytes = 43 characters, Return a random URL-safe text string, in Base64 encoding.

    def hash_refresh_token(self, token: str) -> str:
        """Hash a refresh token before using it as a Redis key."""
        return hashlib.sha256(token.encode()).hexdigest()  # Return the SHA-256 hash of the token as a hexadecimal string. This is used to store the token securely in Redis without exposing the actual token value.

    def create_refresh_session(
        self,
        user_id: int,
    ) -> tuple[str, str, RefreshSession]:
        """Create the initial token, its hash, and a new refresh session. used in login and register endpoints"""

        refresh_token = self.generate_refresh_token()

        now = datetime.now(timezone.utc)

        session = RefreshSession(
            # session_id !=  refresh_token it is just a random id unique for each session usefull when user_id logged in more than one devcie
            session_id=secrets.token_urlsafe(16),
            user_id=user_id,
            absolute_expires_at=now + timedelta(
                days=settings.REFRESH_TOKEN_ABSOLUTE_DAYS
            ),
        )

        refresh_token_hash = self.hash_refresh_token(refresh_token)

        return refresh_token, refresh_token_hash, session


# business rule that is why this calculate_ttl_seconds is here in this service
# Refresh session cannot live beyond absolute expiration.
#  this will be used in token rotation only and this token rotatino will happend only once we call /refresh token endpoint

    def calculate_ttl_seconds(
        self,
        absolute_expires_at: datetime,
    ) -> int:
        """Return the shorter TTL between idle and absolute expiration."""
        # Example: if idle TTL = 30 days but only 10 days remain until the
        # absolute expiry, use 10 days: min(30, 10) = 10.
        # This prevents a refresh session from living past its absolute expiry.
        now = datetime.now(timezone.utc)

        remaining_seconds = int(
            (absolute_expires_at - now).total_seconds()
        )

        idle_seconds = (
            settings.REFRESH_TOKEN_IDLE_DAYS
            * 24
            * 60
            * 60
        )

        return min(idle_seconds, remaining_seconds)

    def create_rotated_token(
        self,
        session: RefreshSession,
    ) -> tuple[str, str, RefreshSession]:
        """Create a replacement token while preserving the session lifetime. used for refresh token endpoint only"""

        refresh_token = self.generate_refresh_token()  # ! created new refresh token

        token_hash = self.hash_refresh_token(
            refresh_token
        )

        new_session = RefreshSession(
            session_id=session.session_id,  # same as the old one
            user_id=session.user_id,  # same no change
            # newly calculated based on remaining time from 90 day
            absolute_expires_at=session.absolute_expires_at,
        )

        return refresh_token, token_hash, new_session

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

    # TTL ( Time To Live ) for resfresh session in Redis is set to 30 days (REFRESH_IDLE_DAYS) to support idle expiration. The absolute expiration is stored in the session object and is set to 90 days (REFRESH_ABSOLUTE_DAYS).
    # We will set TTL for the refresh session in Redis to 30 days (REFRESH_IDLE_DAYS) to support idle expiration. The absolute expiration is stored in the session object and is set to 90 days (REFRESH_ABSOLUTE_DAYS).
