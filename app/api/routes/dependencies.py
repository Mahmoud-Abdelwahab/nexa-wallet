# أي endpoint محمية بتستخدم get_current_user بتمر بالفلو ده:
# Bearer JWT
#     |
#     v
# Signature + exp -> sub + token_version -> User من PostgreSQL
#     |                                       |
#     |                                مقارنة token_version
#     v                                       |
# sid -> Redis: auth_session:user:sid -> refresh_session:current_hash
#                                               |
#                       Active + نفس النسخة؟ -> السماح للـ endpoint
#                       مش Active؟           -> 401 / SESSION_REVOKED
# انتهاء JWT فقط -> ACCESS_TOKEN_EXPIRED؛ عطل Redis -> 503، مش Force Logout.
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
    # الموبايل يعتمد على code الثابتة، مش نص message اللي ممكن يتغيّر أو يترجم.
    "code": "SESSION_REVOKED",
    "message": "Your session is no longer active. Please log in again.",
}


def _load_user_sync(db: Session, user_id: int, token_version: int) -> User:
    # دي حماية على مستوى الحساب: تغيير الباسورد/Logout All يغيّر النسخة لكل الأجهزة.
    # حذف جهاز واحد ما بيغيّرش token_version؛ بيتحكم فيه sid داخل Redis.
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
        # انتهاء الـ Access مش معناه إن الجهاز اتشال؛ الموبايل يجرّب Refresh الأول.
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
    # run_in_threadpool بتبعد استعلام SQLAlchemy الـ sync عن الـ Event Loop.
    # await Redis تحت تفضل على الـ Event Loop لأنها مكتبة async أصلًا.
    sid = payload.get("sid")
    # Older JWTs have no sid and cannot establish a revocable device identity.
    if not isinstance(sid, str) or not sid or len(sid) > 128:
        # JWT قديمة من غير sid ما ينفعش نسمح لها تتجاوز فحص إلغاء الجهاز.
        raise HTTPException(401, SESSION_REVOKED_DETAIL)
    try:
        # نفحص الجلسة في كل طلب، من غير cache محلية تؤخّر اكتشاف الحذف.
        session = await RefreshTokenRedisStore(redis_client).get_active_session(user_id, sid)
    except RedisError:
        # مش عارفين حالة الجلسة أثناء العطل: نرفض الطلب مؤقتًا ونحتفظ بتوكنات العميل.
        # An outage must neither authorize unverified sessions nor force logout.
        raise HTTPException(503, {
            "code": "AUTH_SERVICE_UNAVAILABLE",
            "message": "Authentication is temporarily unavailable. Please retry.",
        }) from None
    if session is None or session.token_version != token_version:
        raise HTTPException(401, SESSION_REVOKED_DETAIL)
    # نحفظ sid اللي اتأكدنا منها في نفس الطلب؛ Logout يستخدمها بدل sid يبعته العميل.
    request.state.session_id = sid
    # الطلب اللي عدى النقطة دي قبل حذف الجلسة ممكن يكمل؛ مش بنلغي شغل بدأ بالفعل.
    return user
