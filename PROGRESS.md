# Nexa Wallet — Progress Log

A running log of everything built in this project so far. Updated after every new step.

---

## 🎯 Project Goal

- Learn Backend development with FastAPI from scratch to Senior Backend Engineer level.
- Build a real portfolio project (Digital Wallet) covering the skills required for modern Backend roles.

## 🧱 Tech Stack

**Backend:** Python 3.13+, FastAPI, SQLAlchemy 2.x, PostgreSQL, Psycopg 3, Redis 7, redis-py asyncio, Pydantic Settings, python-jose, Passlib (bcrypt), Alembic
**Package/Env Manager:** uv
**Local Infrastructure:** Docker Compose (PostgreSQL 16 + Redis 7)
**Frontend (later):** SwiftUI

---

## 🏗️ Core Architecture Decisions

- **Transaction Flow:** `Pending → Bank/Payment Gateway → Success or Failed`. Balance is never debited/credited until the operation succeeds.
- **Ledger:** the final source of truth for balances. The `Transaction` table represents the workflow; `LedgerEntry` represents the actual financial truth (Debit/Credit).
- **Currency:** every monetary record (Transaction, LedgerEntry, Wallet) carries `amount` + `currency` to support multi-currency later.
- **Transaction Type:** `Transfer`, `TopUp`, `Withdraw` (LedgerEntryType additionally has `FEE`, `TAX`, `CASHBACK`).
- **Locking:** row locks are used while executing a transfer, but released before waiting on the bank's response — the lock is released right after the Pending transaction is created.
- **Wallet uniqueness:** `UniqueConstraint(user_id, currency)` — a user can have multiple wallets, but only one per currency.
- **User identity fields:** `email` and `mobile` are unique identifiers. `username` is intentionally **not** unique — it's just a display name, not used to look up or identify a user.

---

## 📚 Concepts Learned

### FastAPI Request Lifecycle
`HTTP Request → Uvicorn → FastAPI → Router → Endpoint → Response`
The Router looks up the matching Endpoint; if no route matches, FastAPI returns 404 automatically.

### Dependency Injection
`Depends(...)` — FastAPI injects dependencies automatically (used for `get_db`, `get_current_user`, and every service).

### Configuration
`app/core/config.py` centralizes all settings (Single Source of Truth) instead of reading `.env` everywhere. `.env` is local/development-only; production should rely on a Secrets Manager (AWS/Azure).

### SQLAlchemy: Engine, Session, Connection Pool
- **Engine**: an app-level singleton, responsible for the Database URL/Driver/Dialect/Pool. It never commits or rolls back.
- **SessionLocal (sessionmaker)**: a factory that creates new Session objects on demand — it does not create a session at definition time.
- **Connection Pool**: a Session only borrows a connection from the pool on its first actual query (**Lazy Connection Acquisition**), not the moment it's created.
- **`get_db()`**: uses `yield` instead of `return` so `db.close()` runs after the endpoint finishes (a dependency with cleanup). `close()` returns the connection to the pool — it does not destroy the Session object.

### ORM / Declarative Base
- **`class Base(DeclarativeBase): pass`** — the official SQLAlchemy 2.0 pattern (not the legacy `declarative_base()`). Every model inherits from it so there's one shared Metadata registry.
- **`Mapped[int]`**: a type annotation telling SQLAlchemy this attribute is an ORM-mapped column, not a plain Python attribute.
- **`mapped_column(...)`**: defines the actual column properties (primary_key, unique, nullable, ForeignKey...).
- **`relationship()` + forward references (`Mapped["Wallet"]`)**: avoid circular imports between model files. Resolution happens later, at mapper-configuration time, via a shared registry that only knows about classes that have actually been imported somewhere — hence all models must be imported together in one `__init__.py`.
- **`TYPE_CHECKING`**: a block that only runs during static analysis (Pylance/mypy), never at runtime — lets the editor resolve forward references without causing a real circular import.

### Alembic (Migrations)
- A migration file is a "recipe" with `upgrade()` and `downgrade()`.
- An `alembic_version` table inside the database tracks the last applied migration.
- `alembic revision --autogenerate -m "..."` diffs current models against the database and generates the difference automatically.
- Before production: it's fine to wipe everything and start from one clean migration. After production: every model change becomes a new migration on top of the previous one — never a reset.

### Authentication & Security
- **Password Hashing (bcrypt via Passlib)**: passwords are hashed before storage and can never be reversed back to plaintext.
- **JWT ≠ Encryption**: the token (`HS256`) is **signed**, not **encrypted**. The Header and Payload are readable by anyone via plain Base64 decoding, no secret required. The `secret_key` is only used to compute/verify the **Signature** (the third part) — proving the token was genuinely issued by the server and untampered, not hiding its contents. Never put sensitive data in a JWT payload.
- **User Enumeration Prevention**: a single generic error message ("Invalid email or password") covers two different cases (email not found / wrong password) so nobody can probe the API to discover which emails are registered.
- **Timing Side-Channel**: even with an identical error message, execution time can still differ (bcrypt is deliberately slow). Fix: always run `verify_password` even when the user doesn't exist, against a fixed `DUMMY_PASSWORD_HASH`, so response time stays roughly constant either way.
- **When to unify an error message vs. not:** unify it only when the difference would leak a server-side/database secret (like the login case). If the difference is something the client already knows (e.g. "no token sent" vs. "sent an invalid token"), there's no need to unify it.
- **`HTTPBearer(auto_error=False)`**: disables FastAPI's automatic behavior (which returns `403`, not `401`, when there's no Authorization header). Once disabled, **you must add the manual check yourself** (`if credentials is None: raise HTTPException(401)`) — otherwise the code crashes (`500`) because `credentials` becomes `None` and nothing handles it.
- **`try/except` only protects the lines physically inside it** — any line before `try:` has no protection at all, even if it looks close in the code. The same applies to code placed *inside* an `except` block after a `raise`: it's unreachable dead code, since `raise` exits immediately.
- **JWT token invalidation via `token_version`**: JWTs are stateless — a valid signature stays valid until expiry no matter what changes server-side (e.g. a password change). To force old tokens to stop working, the `User` model has a `token_version` counter. It's embedded in the JWT payload at login/register time, and `get_current_user` compares it against the user's *current* `token_version` in the database on every request. `change_password` increments `user.token_version`, which immediately invalidates every previously-issued token for that user — they must log in again to get a new one.

### Refresh Tokens and Redis
- Refresh tokens are opaque random values. Only their SHA-256 hashes are stored in Redis.
- `refresh_session:{token_hash}` stores the session JSON with a 30-day idle TTL.
- `user_refresh_sessions:{user_id}` is a Redis Set containing all refresh-token hashes for that user.
- Refresh rotation atomically removes the old hash and session, then stores the new session and hash.
- `logout-all` uses a Lua script and is atomic with rotation, preventing orphaned sessions.
- The session keeps its `session_id`, `user_id`, and absolute expiry during rotation. TTL is limited by the shorter of idle expiry and remaining absolute lifetime (90 days).
- The MVP performs opportunistic cleanup of stale hashes when storing a new session.

#### Why Each Refresh-Token Step Exists
1. **Generate a random opaque token:** the client receives a high-entropy value that does not expose the user id or session data.
2. **Hash the token with SHA-256:** Redis stores only `H(token)`, so a Redis leak does not directly reveal usable refresh tokens.
3. **Store the session by hash:** `refresh_session:{token_hash}` gives a direct lookup from the presented token to its session metadata.
4. **Maintain a per-user Redis Set:** `user_refresh_sessions:{user_id}` makes it possible to find and revoke every device session during logout-all.
5. **Use `SET` plus `SADD`:** `SET` stores the session JSON and applies the individual idle TTL; `SADD` stores the hash in the user's index. They have different responsibilities.
6. **Use a transactional pipeline when storing:** the session key and user index are written together, avoiding an incomplete index when both writes succeed or fail as one transaction.
7. **Hash the incoming refresh token during refresh:** the raw token is never used as a Redis key; only its deterministic hash is looked up.
8. **Load the old session:** a missing key means the token is invalid, revoked, expired, or already consumed.
9. **Calculate the remaining TTL:** `min(idle_ttl, remaining_absolute_lifetime)` prevents Redis from keeping a rotated session beyond its 90-day absolute expiry.
10. **Load the user from `session.user_id`:** the user id comes from trusted session data, not from the request body. We still verify that the user exists before issuing new tokens.
11. **Create a rotated token with the same session identity:** `session_id`, `user_id`, and `absolute_expires_at` remain stable while the raw refresh token and hash change.
12. **Rotate with Lua:** the old key is checked and removed, the old hash is removed from the Set, and the new key/hash are created atomically. This makes the old token single-use and prevents concurrent reuse.
13. **Revoke one device:** delete its session key and remove only its hash from the user Set; other devices remain active.
14. **Revoke all devices with Lua:** the script reads the current Set, deletes every session key, and deletes the Set in one Redis operation. This prevents rotation from creating an orphaned session between `SMEMBERS` and `DEL`.
15. **Increment `token_version` on logout-all:** access JWTs are stateless, so the version check invalidates every old access token immediately without changing per-device logout behavior.
16. **Clean stale hashes opportunistically:** Redis expires individual session keys, but the user Set has no matching TTL. Before a new session is stored, missing session keys are removed from that Set. This is acceptable for the current MVP; a background cleanup job can scale it later.

#### Redis Implementation Naming
The implementation class is named `RefreshTokenRedisStore` because it is the Redis-specific adapter. The name leaves room for another store implementation later, such as a database-backed store.

### FastAPI Response Handling
- **`response_model`**: an actual contract checked at runtime, *after* the business logic has already fully executed (including the DB commit!). It must stay in sync with what the service actually returns, or you get a `ResponseValidationError` (500) **even though the operation already succeeded and was saved to the database**.
- **`status_code`**: set on the route decorator (`@router.post(..., status_code=...)`), not inside the function body. `200 OK` for a synchronous operation that finished immediately, `201 Created` for creating a new resource, `202 Accepted` for an operation that's accepted but will complete later (async) — this will be used for Transfer later.
- **`HTTPException`**: the only way a plain Python exception (like `ValueError`) gets translated into a clear HTTP response for the client, instead of the default generic `500`.

### Separation of Concerns (Service ↔ Router)
- **Service layer**: raises plain Python exceptions (`ValueError`) describing a business-logic problem only — it has no concept of HTTP at all.
- **Router/API layer**: the only place responsible for translating that exception into a status code + response shape (`HTTPException`).
- This separation lets the same Service be reused from anywhere other than HTTP (CLI, background job, tests) without any dependency on FastAPI.
- **Code placed right after a `try/except` block** (same indentation as `try`) only runs on the success path — if the `try` body raises and the `except` re-raises (e.g. as `HTTPException`), execution exits the function immediately and never reaches that code. This is what makes `return {"message": "..."}` after a try/except safe to use as the "operation succeeded" response.

### Partial Updates (PATCH) with Pydantic
- **`request.model_fields_set`**: tells you exactly which fields the client actually sent in the request body, as opposed to which fields simply have a value (a field can have a default/`None` without being "set"). Used in `update_profile` to only touch the fields the client explicitly provided, instead of overwriting everything on every PATCH call.
- **`db.refresh(user)`**: after `commit()`, the in-memory object can still be showing stale data — `refresh()` re-reads the row from the database so the object (and the response built from it) reflects the actual committed state.

---

## 🗂️ Current Project Structure

```
app/
├── core/
│   ├── config.py         # Settings (pydantic-settings, reads .env)
│   ├── enums.py           # Currency, WalletStatus, TransactionStatus
│   └── security.py        # hash_password, verify_password, create/decode_access_token
├── database.py             # Base, engine, SessionLocal, get_db()
├── models/
│   ├── user.py             # User (email, mobile — unique; username is NOT unique)
│   ├── wallet.py           # Wallet (UniqueConstraint(user_id, currency))
│   ├── transaction.py      # Transaction (sender/receiver wallet, status, currency)
│   └── ledger_entry.py     # LedgerEntry (direction, entry_type, amount > 0 check)
├── repositories/
│   ├── user_repository.py  # get_by_username/email/mobile/id, create
│   └── wallet_repository.py
├── mappers/
│   └── user_mapper.py      # User (ORM) → UserResponse (schema)
├── schemas/
│   ├── authentication.py   # RegisterRequest, LoginRequest, UserResponse, AuthResponse
│   ├── updateUserRequest.py     # UpdateUserRequest (partial update)
│   └── changePasswordRequest.py # ChangePasswordRequest
├── services/
│   ├── auth_service.py     # register, login, refresh, rotation, logout
│   ├── refresh_token_service.py # token generation, hashing, TTL, rotation metadata
│   └── user_service.py     # update_profile, change_password
├── infrastructure/
│   └── redis/
│       └── refresh_token_redis_store.py # Redis storage and atomic scripts
├── api/
│   ├── dependencies.py     # get_current_user (JWT auth dependency)
│   ├── authentication.py   # register, login, refresh, logout, logout-all
│   ├── users.py             # GET/PATCH /users/me, POST /users/me/change-password (all protected)
│   └── health.py
└── main.py                  # FastAPI app + include_router

alembic/                     # DB migrations
```

---

## 🔄 Core Flows

### Register
```
REGISTER
   ↓
User + Wallet
   ↓
Commit
   ↓
JWT
```
Details: check for duplicates (mobile, email) → hash the password → create the User → `flush()` (to get the generated id) → create a default AED wallet → `commit()` both together → on any failure, `rollback()` everything → the token is generated only **after** a successful commit.

### Login
```
LOGIN
   ↓
Verify password
   ↓
JWT
   ↓
Client
```
Details: normalize the email (`lower()`) → look it up → verify the password (with timing side-channel protection if the user doesn't exist) → issue a JWT.

### Protected Request
```
Protected Request
   ↓
Authorization: Bearer JWT
   ↓
HTTPBearer
   ↓
decode_access_token()
   ↓
sub
   ↓
UserRepository.get_by_id()
   ↓
Current User
   ↓
UserMapper
   ↓
UserResponse
   ↓
JSON
```
Details: `HTTPBearer(auto_error=False)` extracts the token from the header (returns `None` if missing — handled manually as 401) → `decode_access_token()` verifies the signature and expiry → extract `sub` (user id) → look up the user in the database → if missing or invalid, a unified 401.

### Update Profile (`PATCH /users/me`)
```
UpdateUserRequest
   ↓
model_fields_set (only what the client actually sent)
   ↓
update provided fields (mobile checked for duplicates)
   ↓
Commit + Refresh
   ↓
UserResponse
```

### Change Password (`POST /users/me/change-password`)
```
ChangePasswordRequest
   ↓
verify current_password
   ↓
new_password == confirm_password?
   ↓
new_password != current_password?
   ↓
hash_password(new_password)
   ↓
Commit
   ↓
{"message": "Password changed successfully"}
```

### Refresh Token Rotation
```
Refresh Token A
   ↓ hash
Redis: refresh_session:H(A)
   ↓ load session and validate absolute expiry
Generate Refresh Token B
   ↓ atomic Lua rotation
Delete H(A) + add H(B)
   ↓
New access token + Refresh Token B
```

The old refresh token is single-use. A second refresh attempt with A returns
`401 Unauthorized`. Concurrent requests using the same token produce one
successful rotation and reject the remaining requests.

---

## 🐛 Lessons Learned (Worth Remembering)

- **Name collisions**: importing a name from a library that shadows a name you also use (e.g. `Enum` from sqlalchemy vs. `enum.Enum`, or `Transaction` from sqlalchemy vs. our own model) causes confusing bugs — fix with an explicit alias (`Enum as SQLEnum`) or a fully-qualified `import enum`.
- **Case-sensitive imports**: file/folder names are case-sensitive in Python imports even though macOS's filesystem isn't — they must match exactly, especially since Linux (production) *is* case-sensitive.
- **`create_all()` only creates new tables** — it never alters an existing one. Any change to an existing model needs an Alembic migration (or a drop/recreate, but only during early development).
- **Hidden files and stray spaces in filenames**: a file literally named `" .env"` (with a leading space) is not the same as `.env` — it fails silently because pydantic-settings can't find it.
- **`--reload` and Swagger UI**: the server auto-reloads, but an already-open Swagger tab in the browser can hold a stale schema — refresh the page after every code change before testing.
- **Passlib + modern bcrypt**: `passlib` (last released years ago) is incompatible with newer `bcrypt` releases (5.x) — fix by pinning an older compatible version (`bcrypt<4.1`).

---

## ✅ Current Status

**Done:**
- Models: User, Wallet, Transaction, LedgerEntry + their relationships.
- Database: local PostgreSQL (Homebrew) + Alembic migrations.
- Auth: Register, Login, JWT (issue + decode), password hashing, protection against timing attacks and user enumeration.
- `GET /users/me`, `PATCH /users/me` (partial update), `POST /users/me/change-password` — all JWT-protected via `get_current_user`.
- Redis refresh-token sessions: storage, 30-day idle TTL, 90-day absolute expiry, rotation, reuse detection, and stale-index cleanup.
- Per-device logout and logout-all with `token_version` invalidation.
- Atomic Redis Lua scripts for refresh rotation and logout-all.
- Real integration verification completed against FastAPI, PostgreSQL, and Redis, including concurrent refresh requests.

#### Real Integration Verification
The flow was tested with a live FastAPI server, PostgreSQL, Redis, and real HTTP requests:

1. **Register:** confirmed `201`, access token, refresh token, and default wallet creation.
2. **Login:** confirmed `200`, Redis session key, user Set membership, 30-day TTL, and approximately 90-day absolute expiry.
3. **Protected access:** confirmed a valid JWT returns `200` and an invalid JWT returns `401`.
4. **Refresh and rotation:** confirmed `A -> B`, removal of `H(A)`, insertion of `H(B)`, and preservation of session identity and absolute expiry.
5. **Reuse detection:** confirmed reusing A returns `401`, while B can rotate successfully to C.
6. **Current-device logout:** confirmed the selected refresh token returns `401` while other device sessions remain usable.
7. **Multiple devices:** confirmed logging out device A does not revoke device B.
8. **Logout-all:** confirmed all refresh tokens and old access tokens return `401`, and `token_version` invalidates the old JWTs.
9. **Concurrent refresh:** three simultaneous requests using the same token produced exactly one `200` and two `401` responses, proving atomic rotation.

**Next:** add automated pytest coverage for the verified authentication scenarios, then continue with the remaining CRUD and Wallet APIs (Balance, Transfer, Transaction Workflow).

---

_Last updated: after verifying Redis refresh-token rotation, atomic logout-all, stale-index cleanup, and real concurrent HTTP flows._
