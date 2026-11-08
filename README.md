# Nexa Wallet API

A learning project for a wallet backend built with FastAPI, SQLAlchemy, PostgreSQL
and Redis. It includes authentication, wallet transfers, transaction/ledger history,
and Moyasar sandbox top-ups with idempotency, background processing and reconciliation.

## Run with Docker (no local Python required)

Install and start **Docker Desktop** (or Docker Engine with the Compose plugin).
Clone/download this repository, then open a terminal in the folder containing
`compose.yaml`. Run the following commands from that folder.

### 1. Generate your local settings

macOS / Linux:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "${PWD}:/workspace" -w /workspace python:3.14-slim python scripts/setup_docker_env.py
```

Windows PowerShell:

```powershell
docker run --rm -v "${PWD}:/workspace" -w /workspace python:3.14-slim python scripts/setup_docker_env.py
```

This creates `.env.docker` with random JWT and webhook secrets. Running it again
leaves the existing file unchanged. It does not change your local Python `.env`.
The Unix command creates the file as your own user, so you can edit it afterward.

Share the repository and `.env.docker.example`, **not** `.env` or `.env.docker`.
The real environment files and host virtual environments are excluded from the image.

### 2. Build and start

```bash
docker compose --env-file .env.docker up -d --build
```

The first build downloads images and dependencies. Compose starts PostgreSQL and
Redis, waits for PostgreSQL readiness, runs `alembic upgrade head`, then starts the
API after migrations finish successfully. You do not need to run migrations manually
on first startup. `migrate` showing **Exited (0)** is normal: it is a one-time job.

### 3. Open the API

- Swagger / interactive requests: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- Health: <http://localhost:8000/health>

```bash
docker compose --env-file .env.docker ps -a
docker compose --env-file .env.docker logs -f api
```

Register a new account using `POST /auth/register`, then log in. The default AED
wallet starts at zero; a new installation has no sample users or funded wallets.
Use the schemas shown in Swagger for the current request fields. Transfers and
TopUp creation require an `Idempotency-Key` header: reuse it for retries of the same
request, and choose a new key for a new operation.

The basic API works without a Moyasar key. Real sandbox payment verification needs
your own provider key and the optional payment workers below.

## Enable Moyasar sandbox payments

Edit `.env.docker` locally:

```ini
MOYASAR_SECRET_KEY=sk_test_YOUR_REAL_KEY
```

Keep the generated `MOYASAR_WEBHOOK_SECRET` and use that same value in Moyasar's
webhook settings. These are different secrets. The payment worker rejects the
placeholder key and live keys; a valid sandbox key must come from your account.

Start the full stack, including both workers:

```bash
docker compose --env-file .env.docker --profile payments up -d --build
```

```text
Moyasar webhook endpoint ----+
                            v
Reconciliation producer -> PostgreSQL event queue -> Moyasar consumer
(every cycle + 60s sleep)                            claim -> fetch -> finalize
                                                            |
                                                            v
                                               verify + credit wallet once
```

- `moyasar-worker`: checks payments, schedules retries and posts verified credits.
- `reconciliation-worker`: queues recovery work for PENDING TopUps at least five
  minutes old with no unfinished event. It does not itself credit money.
- PostgreSQL and Redis data live in named volumes and survive a normal shutdown.

Moyasar cannot reach your computer's `localhost`. For webhooks, configure a public
HTTPS endpoint pointing to **`POST /webhooks/moyasar`**. Subscribe to `payment_paid`
and `payment_failed`. Keep any local tunnel running and update Moyasar if its URL
changes. Webhook verification codes appear in the API logs.

Optional webhook-only Cloudflare tunnel, in two separate host terminals (requires
local Python and Cloudflare's `cloudflared` command):

```bash
# Terminal 1: forward only POST /webhooks/moyasar to the Docker API on port 8000.
python3 scripts/run_webhook_proxy.py
```

The proxy listens on `127.0.0.1:8001`; the API itself stays bound to localhost.

```bash
# Terminal 2: expose only the proxy.
cloudflared tunnel --url http://127.0.0.1:8001
```

Set Moyasar's URL to `https://YOUR-TUNNEL.trycloudflare.com/webhooks/moyasar`.
The proxy example assumes `API_PORT=8000`. See the [TopUp flow](MOYASAR_TOPUP_FLOW.md)
for creating a payment with the returned Payment ID, amount and currency. Docker
startup does not create a payment or run a mobile checkout automatically.

## Logs, support alerts and stopping

```bash
# API requests, exceptions and webhook verification codes
docker compose --env-file .env.docker logs -f api

# Payment processing and reconciliation (after enabling the payments profile)
docker compose --env-file .env.docker logs -f moyasar-worker reconciliation-worker

# Read durable support alerts; this does not retry or change payments
docker compose --env-file .env.docker exec api python scripts/list_payment_support_alerts.py --limit 50

# Stop the full stack, preserving saved data
docker compose --env-file .env.docker --profile payments down
```

`Ctrl+C` while following `logs -f` closes the log viewer, not the containers.
`docker compose down -v` also deletes volumes and their data; do not use it to
perform a normal stop.

After code changes, rebuild with `up -d --build`. After changing `.env.docker`, run
`up -d` to recreate affected containers; `restart` alone does not reload environment
variables. Include `--profile payments` if you use the workers. Do not run old host
workers against the same database alongside the new container workers.

## Run automated tests inside Docker

```bash
docker compose --env-file .env.docker build tests
docker compose --env-file .env.docker run --rm --no-deps tests
```

These tests use isolated SQLite data and simulated provider responses. They do not
create real payments or use your Moyasar key. The runtime image excludes pytest;
the separate test target installs the locked development dependencies.

Run only the beginner example:

```bash
docker compose --env-file .env.docker run --rm --no-deps tests python -m pytest tests/test_amount.py -v -o cache_dir=/tmp/pytest-cache
```

Real PostgreSQL locking tests are separate; see `scripts/test_topup_concurrency.py`
and [test results](TOPUP_TEST_RESULTS.md). SQLite tests do not prove row-lock behavior.

## Ports and troubleshooting

| Service | On your computer | Inside Compose |
| --- | --- | --- |
| API | `127.0.0.1:8000` | `api:8000` |
| PostgreSQL | `127.0.0.1:5433` | `postgres:5432` |
| Redis | `127.0.0.1:6379` | `redis:6379` |

- **Port already in use:** stop your old local API, or change `API_PORT` in
  `.env.docker` (for example `8002`). The same applies to `POSTGRES_PORT` and
  `REDIS_PORT`. Open the corresponding port in your browser.
- **Existing Docker database:** the default project name follows the directory
  name. Using this repository in the same directory reuses its existing
  `postgres_data` volume. Use a different `-p PROJECT_NAME` plus different host
  ports for an isolated copy, and use that project name on every Compose command.
- **API does not start:** inspect `docker compose --env-file .env.docker logs migrate api`.
  A migration failure blocks API/worker startup; fix it instead of deleting data.
- **Worker exits:** check its logs and the real sandbox key. The worker cannot
  generate a valid provider credential.
- **TopUp stays PENDING:** inspect both worker logs and verify the public webhook
  URL. `404`/waiting payments remain pending and are checked later. Only a verified
  matching paid payment can credit the wallet.
- **Changed code has no effect:** images contain a copy of the code; rebuild them.
  This setup does not bind-mount your source or enable auto-reload.

This Compose stack is for local learning and sandbox demos. Database credentials
are local-development defaults and published ports are bound to loopback. Public
production hosting, TLS, backups and support notification delivery are separate work.

## Local Python development (optional)

Python 3.14 and `uv` are used by the Docker build. To work outside Docker:

```bash
uv sync --locked
```

Use a local `.env` containing `SECRET_KEY`, `MOYASAR_SECRET_KEY`, optional
`MOYASAR_WEBHOOK_SECRET`, `DATABASE_URL` and `REDIS_URL`. Local URLs use `localhost`
and the published host ports; container URLs use `postgres` and `redis`.
Start only the database services if the API will run on your host:

```bash
docker compose --env-file .env.docker up -d postgres redis
DEBUG=false .venv/bin/python -m alembic upgrade head
DEBUG=false .venv/bin/python -m uvicorn app.main:app --reload
```

## Docker verification

Validated on 2026-11-08 in an isolated Docker project: fresh PostgreSQL migrations,
API health/docs, account registration, login, zero-balance wallet lookup and Redis
refresh-token rotation. Both workers started against an empty queue using dummy
sandbox-shaped credentials; this was not a real Moyasar payment test.
The Linux test container passed 79 tests (two dependency deprecation warnings).
Existing local databases and payment credentials were not used.

## API and learning references

| Area | Main routes |
| --- | --- |
| Authentication | `/auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/logout-all` |
| Users | `/users/me`, `/users/me/change-password` |
| Wallets | `/wallets`, `/wallets/{id}`, `/wallets/{id}/ledger` |
| Transfers | `POST /transfers` |
| Transactions | `GET /transactions`, `GET /transactions/{id}` |
| TopUps | `POST /topups`, `GET /topups/{id}` |
| Webhooks | `POST /webhooks/moyasar` |

- [Business rules and table relationships](BUSINESS_GUIDE.md)
- [Moyasar TopUp flow](MOYASAR_TOPUP_FLOW.md)
- [Worker, retries, leases and support alerts](MOYASAR_WORKER_GUIDE.md)
- [Reconciliation](RECONCILIATION_GUIDE.md)
- [Test results](TOPUP_TEST_RESULTS.md) and [progress log](PROGRESS.md)

Implementation references: [Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/)
and [locked uv dependencies in Docker](https://docs.astral.sh/uv/guides/integration/docker/).
