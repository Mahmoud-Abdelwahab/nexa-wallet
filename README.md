# Nexa Wallet API

A digital wallet API built with FastAPI, SQLAlchemy, PostgreSQL, and Redis.

## Features

- User registration and login with JWT access tokens
- Opaque refresh tokens stored and rotated in Redis
- Per-device logout and logout-all revocation
- Token-version invalidation for all old access tokens
- Wallet creation and transaction domain models
- PostgreSQL persistence with Alembic migrations

## Tech Stack

- **FastAPI** - HTTP API framework
- **SQLAlchemy 2.0** - ORM and database access
- **PostgreSQL 16** - Primary database
- **Redis 7** - Refresh-session storage and atomic token rotation
- **Pydantic** - Request and response validation
- **python-jose** - JWT signing and validation
- **Passlib + bcrypt** - Password hashing
- **Uvicorn** - ASGI server
- **Docker Compose** - Local PostgreSQL and Redis services

## Getting Started

### Prerequisites

- Python 3.13+
- Docker and Docker Compose
- uv (recommended) or pip

### Installation

1. Create and activate a virtual environment:

```bash
uv venv
source .venv/bin/activate
```

2. Install the project:

```bash
uv pip install -e .
```

3. Configure environment variables in `.env`. At minimum, set `SECRET_KEY`.

4. Start PostgreSQL and Redis:

```bash
docker compose up -d
```

5. Apply database migrations:

```bash
alembic upgrade head
```

6. Start the API:

```bash
uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`.

### API Documentation

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Project Structure

```text
nexa-wallet/
├── app/
│   ├── api/                 # Authentication, user, and health routes
│   ├── core/                # Configuration, security, and Redis client
│   ├── infrastructure/      # Redis store implementation
│   ├── mappers/             # ORM-to-response mapping
│   ├── models/              # SQLAlchemy models
│   ├── repositories/        # Database access layer
│   ├── schemas/             # Pydantic request/response schemas
│   ├── services/            # Authentication and user business logic
│   ├── database.py          # Engine, session factory, and DB dependency
│   └── main.py              # FastAPI application
├── alembic/                 # Database migrations
├── compose.yaml             # PostgreSQL and Redis services
├── pyproject.toml
├── PROGRESS.md
└── README.md
```

## API Endpoints

### Authentication

- `POST /auth/register` - Register a user and create the default AED wallet
- `POST /auth/login` - Issue an access token and refresh token
- `POST /auth/refresh` - Rotate a refresh token and issue new tokens
- `POST /auth/logout` - Revoke the current device session
- `POST /auth/logout-all` - Revoke all refresh sessions and invalidate old access tokens

### Users

- `GET /users/me` - Get the authenticated user
- `PATCH /users/me` - Update the authenticated user's profile
- `POST /users/me/change-password` - Change password and invalidate old access tokens

### Health

- `GET /health` - Health check endpoint

## Refresh Token Design

Refresh tokens are random opaque values. The raw token is returned to the client, but only its SHA-256 hash is stored in Redis.

```text
refresh_session:{token_hash}
    -> session JSON with user_id, session_id, and absolute_expires_at
    -> idle TTL: 30 days

user_refresh_sessions:{user_id}
    -> Redis Set containing all refresh-token hashes for that user
```

During rotation, Redis atomically removes the old session and hash, then creates the new session and adds the new hash. The old refresh token is single-use. `logout-all` uses a separate Lua script and is atomic with rotation, so it cannot leave an orphaned rotated session.

## Environment Variables

| Variable | Description | Default |
| --- | --- | --- |
| `APP_NAME` | Application name | `Nexa Wallet` |
| `DEBUG` | Debug mode | `True` |
| `DATABASE_URL` | PostgreSQL connection URL | `postgresql+psycopg://nexa:nexa_password@localhost:5433/nexa_wallet_dev` |
| `SECRET_KEY` | JWT signing secret | Required |
| `ALGORITHM` | JWT algorithm | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Access-token lifetime | `30` |
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` |
| `REFRESH_TOKEN_IDLE_DAYS` | Refresh session idle lifetime | `30` |
| `REFRESH_TOKEN_ABSOLUTE_DAYS` | Refresh session absolute lifetime | `90` |

## Verification

The authentication flow has been verified with real HTTP calls against FastAPI, PostgreSQL, and Redis. Verified scenarios include:

- Registration and login
- Redis session keys, user session Sets, TTL, and absolute expiry
- Protected endpoint access and invalid access tokens
- Refresh rotation and old-token reuse rejection
- Per-device logout and multiple-device behavior
- Logout-all and `token_version` invalidation
- Concurrent refresh protection: one success and the remaining requests rejected
- Stale Redis session-index cleanup

## Local Services

```bash
docker compose up -d       # Start PostgreSQL and Redis
docker compose down        # Stop services and preserve volumes
docker compose down -v     # Stop services and remove database volumes
```

## Development Commands

```bash
alembic upgrade head
pytest
ruff format .
ruff check .
```
