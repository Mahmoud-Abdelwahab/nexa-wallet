# Nexa Wallet — Implementation and Handoff Status

Last updated: **2026-11-15 (Asia/Dubai)**.
Working branch: **`adding-new-support-apis`**.

This file describes the current source and the checks actually performed. Older
transfer/payment reports remain useful historical evidence, but are not substitutes
for the latest implementation or a final production review.

## Current state

The planned backend MVP features are implemented and ready for teammate setup,
local demos and technical review. This is a sandbox/portfolio backend; the mobile
application and final production integration/security review remain outstanding.

| Area | Status | Evidence / reference |
| --- | --- | --- |
| User registration/login/profile | Implemented | Auth routes, execution-boundary/error tests and fresh Docker HTTP checks |
| Access and refresh credentials | Implemented | JWT version checks, refresh rotation, Redis Lua and session tests |
| Password recovery | Implemented | Single-use reset tokens, SMTP, email/IP limits, failure-path tests and real Mailpit flow |
| Wallet ownership and currencies | Implemented | AED/USD/EUR, scoped reads and one-wallet-per-user/currency constraint |
| Internal transfers | Implemented | Idempotency, shared lock order, Decimal validation, paired ledger entries and one commit |
| Transaction and ledger history | Implemented | Owned history and paginated ledger entries |
| Moyasar sandbox top-ups | Implemented | Idempotent creation, durable events, verification worker, leases, retries and reconciliation |
| Recipient verification | Implemented | Masked username, ownership/status/currency checks, 5/minute and 50/24-hour user limits |
| Device sessions | Implemented | `sid`, active-pointer checks, `is_current`, current-session deletion guard and cross-device revocation |
| Wallet Summary + FX | Implemented | Decimal math, reference dates, six-hour cache, live AED/USD/EUR provider/cache verification |
| Statement Export | Implemented | Owned CSV, inclusive dates, 366-day/5,000-row limits, escaping and attachment headers |
| Public About + Support | Implemented | `/app/about`, `/app/support`, validated optional contacts and six HTTP/configuration tests |
| Docker handoff | Verified locally | Fresh isolated services/migrations, Linux test image and live HTTP flow |
| Mobile client | Not implemented here | Follow the documented JWT/session error contract and feature APIs |
| Final integration/security release review | Pending | Target-environment review and remaining items below |

Support is a public contact-information endpoint, not a support-ticket submission
system. At the owner's request, the environment template uses `support@example.com`
and `https://example.com/support` as dummy UI-test contacts. They do not reach a
Nexa support service. Empty `SUPPORT_EMAIL`/`SUPPORT_URL` produce `available: false`;
replace the demo values with actual public contacts before real use.

## Latest verification — 2026-11-15

### Automated tests

| Run | Result | Scope |
| --- | --- | --- |
| Local `DEBUG=false .venv/bin/python -m pytest tests -q` | **158 passed, 13 skipped** | Unit/HTTP tests; optional real Redis tests intentionally disabled |
| Fresh Docker test image with both Redis test URLs enabled | **171 passed, no skips** | Includes real Redis session/rate-limit scripts, API authorization and race checks |
| `scripts/test_topup_concurrency.py --run-local` on disposable PostgreSQL | **Passed** | Concurrent posting/creation, conflicting requests and lease recovery with fake provider data |

The suite also reported 62 passing subtests. Two dependency deprecation warnings
remain: an anyio pytest-plugin alias and Starlette's httpx TestClient integration.
No failing tests were observed in these runs. Tests ran locally; this is not a
claim that a GitHub CI test workflow passed. The current GitHub workflow performs
Codex PR review and requires its configured repository secret.

### Fresh Docker startup and HTTP smoke checks

A separate Compose project (`nexa-handoff-check`) used fresh PostgreSQL/Redis
volumes and alternate localhost ports. Existing user databases, wallets and
provider credentials were not used. Migrations and API startup succeeded.

Verified using disposable accounts over HTTP:

1. Health, Redis diagnostic, Swagger, public About and unconfigured Support (the
   isolated smoke run used blank contacts; demo contacts were configured afterward).
2. Registration, default zero-balance AED wallet and active-session profile access.
3. Creation of USD/EUR wallets and live reference FX summary for all three wallets.
4. CSV download with BOM and `no-store`, and cross-user download rejection.
5. Masked recipient lookup and rejection of an unfunded transfer.
6. `is_current`, current-session deletion protection, remote device revocation and
   immediate `SESSION_REVOKED` when the deleted device attempted CSV download.
7. Refresh rotation and successful use of the replacement access token.
8. Actual SMTP delivery into local Mailpit, single-use reset-token consumption,
   account-wide invalidation and login with the new password.

The disposable stack is removed after verification. SMTP was local-only. The FX
request used the public provider, but payment-provider responses in posting tests
were mocked. This did not charge a card, prove live Moyasar settlement or complete
a production security audit.

### PostgreSQL concurrency checks

The existing opt-in test created a temporary schema and removed it afterward:

- Two workers with distinct events for one payment produced one Transaction and
  one ledger credit, with balance moving from 50 to 150 exactly once.
- Three concurrent identical TopUp creations returned one TopUp and Payment ID.
- Concurrent requests with the same key and changed data produced one winner and
  one conflict, without a credit.
- Expired leases were reclaimed and stale workers could not post a credit.

Previous transfer/PostgreSQL verification is recorded in
[TRANSFER_TEST_RESULTS.md](TRANSFER_TEST_RESULTS.md). The latest handoff smoke
check exercised insufficient-funds rejection, not a new funded-transfer race run.

## Current architecture

```text
Client
  |
  +-- /app/about, /app/support ----------------> explicit public display fields
  |
  +-- protected API -> JWT signature/expiration
                           |
                       PostgreSQL user/version check (worker thread)
                           |
                       Redis sid -> current refresh record (atomic read)
                           |
                       Router -> Service -> Repository -> PostgreSQL
                                      |
                                      +-- async Redis: sessions, limits, FX cache
                                      +-- external reference FX / SMTP as required

Money movement:
validate -> idempotency -> lock relevant records -> ledger + balance -> one commit
Failure -> rollback

External payments:
webhook -> durable PostgreSQL queue -> worker fetch/verify -> post once
                                   ^
                                   |
                           reconciliation producer
```

Synchronous database/password work runs in worker threads. Redis and async HTTP
remain on the event loop. Services/repositories preserve the existing project
structure; simple public app metadata needs neither a database nor a service layer.

## Important contracts for reviewers

### Identity and sessions

- Email/mobile identify users; `username` is a non-unique display name.
- Every successful login creates a new random `sid`; it identifies a login
  session, not physical hardware. Labels are untrusted display metadata.
- Rotation preserves `sid`, creation time, device metadata and absolute expiry;
  it replaces the refresh token and updates last-seen time.
- Redis keys are `refresh_session:{hash}`, `user_refresh_sessions:{user_id}` and
  `auth_session:{user_id}:{sid}`. Record/index/pointer updates are atomic.
- Protected requests check the current account version and active session. A
  deleted device cannot keep using an otherwise unexpired access JWT.
- `401 / SESSION_REVOKED` is terminal for the client; expired access JWTs instead
  use `ACCESS_TOKEN_EXPIRED`. A Redis auth outage returns retryable 503.
- Current-session deletion returns `400 / CURRENT_SESSION_DELETE_FORBIDDEN`;
  `/auth/logout` signs out the current session. Logout-all changes the account
  version and revokes all indexed sessions.
- Old pre-sid credentials require login after rollout. An already-authorized
  in-flight request may finish; no push/offline-dialog implementation is included.

### Financial records and external integrations

- Wallet currencies are AED, USD and EUR. A user has at most one wallet per currency.
- Transfer posting changes balances and paired ledger entries in one transaction,
  with stable lock ordering and idempotent replay/conflict behavior.
- The Ledger records actual movements; pending external payment requests alone
  do not credit money. Checkout requires the client/provider integration.
- TopUp credit requires independently fetched matching paid provider data, with
  the account's configured supported currency and sandbox credential.
- Worker retry exhaustion creates a durable support alert, not a user-facing
  contact ticket. Reconciliation can recover pending payment work.
- FX is for estimated display, never settlement. Missing/stale/invalid rates fail
  the whole summary rather than returning a partial total.
- CSV exports preserve amounts/currencies and do not mutate financial records.
  They are ledger activity downloads, not certified opening/closing statements.
- Existing ledger timestamps are naive. CSV preserves their stored time semantics;
  it does not silently convert historical timestamps into a new timezone.

## Review and release work remaining

These are review tasks, not features falsely marked as complete:

- Complete the final target-environment integration/security review, including
  authentication abuse controls, password-policy/hash limits, permissions, logs,
  proxy configuration and failure/recovery behavior.
- Review concurrent wallet creation: the database uniqueness constraint exists,
  but the wallet service still relies on a precheck without mapping every possible
  concurrent uniqueness failure to a friendly conflict response.
- Review balance protection: services validate funds, but `Wallet.balance` does
  not currently have a database-level non-negative check constraint.
- Decide and validate a consistent timezone policy before changing naive stored
  timestamps or presenting timezone-dependent statements in the mobile client.
- Configure real public support contacts and delivery credentials for the intended
  environment. Password-reset background email has no durable retry queue.
- Verify real sandbox checkout/provider callbacks using the team's own account;
  mock-based posting tests are not a payment-provider certification.
- Build the mobile screens, secure token storage, coordinated refresh interceptor,
  session-ended dialog and CSV save/share flow.
- For public deployment, configure TLS, private service networking, managed
  secrets, backups/restore, monitoring and operational ownership. The supplied
  Compose stack is a local/sandbox setup.
- Commit/push the agreed review snapshot before teammates clone it. Current
  local verification does not publish a hosted API, merge a PR or distribute code
  automatically. Validate on any additional target OS/CPU platform as required.

## Milestones

| Date | Implemented / verified |
| --- | --- |
| 2026-09 to 2026-10 | User/wallet models, authentication, profiles, refresh rotation and wallet reads |
| 2026-10-25 to 2026-10-31 | Internal transfer/ledger flow, idempotency, execution boundaries and API error privacy |
| 2026-11-07 to 2026-11-08 | Moyasar durable events, worker verification, creation idempotency, leases, retries and reconciliation |
| 2026-11-08 | Initial shareable Docker setup; historical Linux run: 79 tests |
| 2026-11-14 to 2026-11-15 | Password reset, recipient lookup, device metadata/sid and current-session protection |
| 2026-11-15 | Wallet Summary/FX, Statement Export, English inline comments and public About/Support |
| 2026-11-15 | Refreshed README/progress and fresh Docker handoff validation: 171 tests plus HTTP/PostgreSQL checks |

## Documentation map

Start with [README.md](README.md) for cloning, environment generation, startup,
configuration, API inventory, tests and reviewer instructions.

- [Business/design learning guide](BUSINESS_GUIDE.md)
- [Device sessions and mobile error contract](DEVICE_SESSIONS_GUIDE.md)
- [Wallet Summary + FX](WALLET_SUMMARY_GUIDE.md)
- [Statement Export](WALLET_STATEMENT_GUIDE.md)
- [Moyasar TopUp flow](MOYASAR_TOPUP_FLOW.md)
- [Worker and durable support alerts](MOYASAR_WORKER_GUIDE.md)
- [Reconciliation](RECONCILIATION_GUIDE.md)
- [Earlier TopUp test evidence](TOPUP_TEST_RESULTS.md)
- [Earlier transfer test evidence](TRANSFER_TEST_RESULTS.md)

Learning notes may describe decisions at the time they were written. For the
current implementation status, use this file, the API schema and the source.
