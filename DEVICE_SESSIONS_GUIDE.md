# Device sessions and immediate access revocation

للقراءة مع الكود، امشي بالترتيب ده. أضفت شرح بالعربي ورسومات نصية داخل الملفات:

| الملف | هتفهم منه إيه؟ |
| --- | --- |
| [device_session.py](app/schemas/device_session.py) | بيانات الجهاز وحدودها والبيانات الآمنة اللي بنعرضها |
| [authentication schema](app/schemas/authentication.py) | إزاي Login وRegister بيورثوا بيانات الجهاز الاختيارية |
| [refresh_session.py](app/schemas/refresh_session.py) | الفرق بين هوية الجلسة والتوكن والتواريخ |
| [authentication routes](app/api/routes/authentication.py) | دخول الطلب وتوصيله للخدمة وأكواد رد الموبايل |
| [auth_service.py](app/services/auth_service.py) | ربط Login/Refresh/Logout بنفس sid |
| [refresh_token_service.py](app/services/refresh_token_service.py) | إنشاء جلسة جديدة وتجهيز Rotation من غير تغيير هويتها |
| [Redis store](app/infrastructure/redis/refresh_token_redis_store.py) | رسمة المفاتيح الثلاثة وشرح Lua ومنع التداخل بين الحذف والتجديد |
| [dependencies.py](app/api/routes/dependencies.py) | رسمة فحص JWT ونسخة الحساب وsid عند كل طلب محمي |
| [device_sessions_service.py](app/services/device_sessions_service.py) | تنظيف وفحص وعرض جلسات المستخدم وحذف جهاز واحد |
| [test_device_sessions.py](tests/test_device_sessions.py) | اختبارات الجهاز المحذوف والتزامن وحالات 401 و503 |

ابدأ برسمة Redis Store، وبعدها رسمة `dependencies.py`، وبعدها تابع Login والـ
Rotation في الخدمات. كده هتربط سبب تخزين كل مفتاح بطريقة استخدامه في الطلبات.

Session endpoints live with the other authentication routes in
`app/api/routes/authentication.py`. This feature uses the existing PostgreSQL
users and Redis, with no new database table or migration.

## Login and registration

Both requests accept optional display metadata:

```json
{
  "email": "alex@example.com",
  "password": "YourPassword123!",
  "device_name": "Samsung S24",
  "platform": "Android"
}
```

Registration still requires its existing username, mobile and date-of-birth
fields. `device_name` allows 1–100 characters and defaults to `Unknown device`.
`platform` accepts `iOS`, `Android`, `Web` or `Unknown`, defaulting to `Unknown`.
These client-provided labels do not prove a device's identity.

Each successful login/registration creates a new random session ID. The signed
access JWT contains `sub`, `token_version`, `sid` and `exp`. Its `sid` identifies
one login session, not a physical device; separate logins on the same device can
appear as separate sessions.

## List and delete sessions

Call `GET /auth/sessions` with the access token:

```json
[
  {
    "session_id": "RANDOM_SESSION_ID",
    "is_current": true,
    "device_name": "Samsung S24",
    "platform": "Android",
    "created_at": "2026-11-15T09:00:00Z",
    "last_seen_at": "2026-11-15T10:30:00Z"
  }
]
```

Only the authenticated user's active sessions at the current `token_version`
are returned. Refresh tokens, token hashes, user IDs and credentials are omitted.
`is_current` compares each session ID to the verified access JWT's `sid`. The mobile
app should label this session as the current device and hide its delete action.
It remains current after refresh rotation because `sid` does not change.
Expired index entries are cleaned up. `last_seen_at` updates on refresh rotation,
not on every API request.

Call `DELETE /auth/sessions/{session_id}` using a remaining active device's token:

- Another session found and owned by the caller: `204 No Content`.
- Missing, already deleted or owned by someone else: `404 Session not found`.
- Deleting the caller's current session: `400` with
  `detail.code=CURRENT_SESSION_DELETE_FORBIDDEN`. The session stays active. This
  server-side check is enforced even if the mobile delete button is bypassed.
  Use `POST /auth/logout` to exit the current device instead.

Deleting one session does not increment the user's `token_version` or revoke
other sessions. Logout revokes the current verified `sid`, even if the body
contains an old rotated refresh token. Logout-all and password changes retain
the existing account-wide version invalidation.

## How Redis keeps access and refresh revocation consistent

```text
user_refresh_sessions:{user_id}  -> SET of refresh-token hashes
refresh_session:{token_hash}    -> session JSON and idle/absolute expiry
auth_session:{user_id}:{sid}    -> current refresh-token hash, with matching TTL
```

Initial storage writes the record, user index and stable `sid` pointer in one
Redis transaction. Rotation updates all of them atomically in Lua, preserving
`sid`, device labels, `created_at`, version and absolute expiration, and updating
`last_seen_at`. Pointer TTL is capped at both idle and absolute expiry.

Every protected request validates the JWT and the PostgreSQL user's version,
then checks the `sid` pointer and its live refresh record atomically in Redis.
The check follows the pointer to the current hash, so rotating a refresh token
does not invalidate an otherwise unexpired access JWT from the same session.
This adds a Redis lookup to every authenticated request; there is no local cache
that would delay revocation.

Session deletion and rotation are atomic relative to each other. If rotation
wins first, deletion removes the rotated record; if deletion wins first,
rotation cannot recreate it. Other devices remain active.

Requests authenticating after revocation are rejected. A request that already
passed authentication before deletion may finish; this does not cancel in-flight
database transactions.

## Mobile error contract

A deleted, expired or account-invalidated session returns:

```http
HTTP/1.1 401 Unauthorized
```

```json
{
  "detail": {
    "code": "SESSION_REVOKED",
    "message": "Your session is no longer active. Please log in again."
  }
}
```

This code also covers invalid/replayed refresh tokens: the mobile client must
serialize refresh requests and retry waiting requests with the single refreshed
token pair, rather than submit competing refreshes.

Suggested centralized mobile interceptor:

```text
if status == 401 and detail.code == "SESSION_REVOKED":
    cancel queued authenticated retries
    clear stored access and refresh tokens
    show the session-ended dialog once
    navigate to login

if status == 401 and detail.code == "ACCESS_TOKEN_EXPIRED":
    perform one coordinated refresh
    on success: save the new token pair and retry the original request once
    on SESSION_REVOKED: perform the logout flow above

if status == 503 and detail.code == "AUTH_SERVICE_UNAVAILABLE":
    keep tokens and allow a later retry
```

No push notification is needed for this request-driven flow. An idle or offline
app cannot discover revocation until it makes its next protected request; server
responses alone cannot proactively open an offline app's dialog. The mobile
dialog/interceptor itself is still to be implemented in the mobile application.

## Existing sessions after this update

Old access tokens have no `sid` and old refresh records have no active pointer.
They are deliberately rejected rather than allowed to bypass revocation. Existing
clients must log in again once after deploying this change. Old records expire
normally; no migration or Redis flush is required.

## Verify the flow

1. Restart/rebuild the API. Log in twice with the same account and different
   device labels, keeping both returned token pairs.
2. From device A, call `GET /auth/sessions` and copy device B's `session_id`.
3. From A, delete B using `DELETE /auth/sessions/{session_id}`; expect 204.
4. From B, call `/users/me` or `/wallets` with its existing access token; expect
   401 and `detail.code == SESSION_REVOKED`.
5. From B, try `/auth/refresh`; expect the same terminal code.
6. From A, call `/users/me`; it must still succeed.

Automated tests:

```bash
DEBUG=false .venv/bin/python -m pytest tests/test_device_sessions.py -q
# Optional integration checks against a dedicated test Redis:
DEBUG=false DEVICE_SESSIONS_TEST_REDIS_URL=redis://127.0.0.1:16387/0 .venv/bin/python -m pytest tests/test_device_sessions.py -q
```

Integration tests create isolated SQLite users, use real Redis Lua scripts, test
HTTP authorization and refresh, and remove their generated session keys. They
cover device isolation, current-device identification and deletion protection,
logout, ownership, expiry, stale indexes,
rotation/revocation races, and concurrent access checks during rotation.
