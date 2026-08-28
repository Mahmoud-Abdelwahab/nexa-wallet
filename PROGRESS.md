# Nexa Wallet — Progress Log

A running log of everything built in this project so far. Updated after every new step.

---

## 🎯 Project Goal

- Learn Backend development with FastAPI from scratch to Senior Backend Engineer level.
- Build a real portfolio project (Digital Wallet) covering the skills required for modern Backend roles.

## 🧱 Tech Stack

**Backend:** Python 3.13+, FastAPI, SQLAlchemy 2.x, PostgreSQL, Psycopg 3, Pydantic Settings, python-jose, Passlib (bcrypt), Alembic
**Package/Env Manager:** uv
**Later:** Docker, Redis, Pytest
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
│   ├── auth_service.py     # register_user, login_user (business logic)
│   └── user_service.py     # update_profile, change_password
├── api/
│   ├── dependencies.py     # get_current_user (JWT auth dependency)
│   ├── authentication.py   # POST /auth/register, POST /auth/login
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

**Next:** per the original roadmap — remaining CRUD and Wallet APIs (Balance, Transfer, Transaction Workflow).

---

_Last updated: after adding `PATCH /users/me` and `POST /users/me/change-password`, and removing the unique constraint on `username`._
