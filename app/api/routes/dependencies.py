# Every protected endpoint using get_current_user follows this flow:
# Bearer JWT
#   |
#   v
# Verify signature and expiration
#   |
#   v
# Load PostgreSQL user and compare token_version (worker thread)
#   |
#   v
# sid -> Redis pointer -> current refresh record (one atomic read)
#   |
#   +-- Active and matching version --> allow the endpoint
#   +-- Missing or inactive ---------> 401 / SESSION_REVOKED
# Expired JWT -> ACCESS_TOKEN_EXPIRED; Redis outage -> 503, not forced logout.
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose.exceptions import ExpiredSignatureError
from redis.exceptions import RedisError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.redis import redis_client
from app.core.security import decode_access_token
from app.database import get_db
from app.infrastructure.redis.refresh_token_redis_store import RefreshTokenRedisStore
from app.models.user import User
from app.repositories import UserRepository


bearer_scheme = HTTPBearer(auto_error=False)
SESSION_REVOKED_DETAIL = {
    # Mobile clients match this stable code, not message text that may change or be translated.
    "code": "SESSION_REVOKED",
    "message": "Your session is no longer active. Please log in again.",
}


def _load_user_sync(db: Session, user_id: int, token_version: int) -> User:
    # Account-wide check: password changes and logout-all increment the version for every device.
    # Deleting one session leaves token_version unchanged; its Redis sid controls revocation.
    user = UserRepository(db).get_by_id(user_id)
    if user is None:
        raise HTTPException(401, "Invalid authentication credentials")
    if user.token_version != token_version:
        raise HTTPException(401, SESSION_REVOKED_DETAIL)
    return user


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = decode_access_token(credentials.credentials)
    except ExpiredSignatureError:
        # An expired access token does not imply revocation; the client should attempt refresh first.
        raise HTTPException(401, {
            "code": "ACCESS_TOKEN_EXPIRED",
            "message": "Access token expired. Refresh your session.",
        }) from None
    except Exception:
        raise HTTPException(401, "Invalid authentication credentials") from None

    try:
        user_id = int(payload["sub"])
        token_version = int(payload["token_version"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(401, "Invalid authentication credentials") from None

    # SQLAlchemy stays in a worker; the async Redis client stays on the loop.
    user = await run_in_threadpool(_load_user_sync, db, user_id, token_version)
    # run_in_threadpool moves synchronous SQLAlchemy queries off the event loop.
    # The async Redis client below stays on the event loop.
    sid = payload.get("sid")
    # Older JWTs have no sid and cannot establish a revocable device identity.
    if not isinstance(sid, str) or not sid or len(sid) > 128:
        # Legacy JWTs without sid must not bypass per-session revocation checks.
        raise HTTPException(401, SESSION_REVOKED_DETAIL)
    try:
        # Check Redis on every request; a local cache would delay detection of revocation.
        session = await RefreshTokenRedisStore(redis_client).get_active_session(user_id, sid)
    except RedisError:
        # During an outage the session state is unknown: reject temporarily and keep the client's tokens.
        # An outage must neither authorize unverified sessions nor force logout.
        raise HTTPException(503, {
            "code": "AUTH_SERVICE_UNAVAILABLE",
            "message": "Authentication is temporarily unavailable. Please retry.",
        }) from None
    if session is None or session.token_version != token_version:
        raise HTTPException(401, SESSION_REVOKED_DETAIL)
    # Save the verified sid for this request; logout uses it instead of a client-supplied sid.
    request.state.session_id = sid
    # A request authenticated before revocation may finish; this does not cancel work already authorized.
    return user
