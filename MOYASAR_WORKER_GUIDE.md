# Moyasar Worker and TopUp Models — Learning Guide

Read this alongside the [six-step business flow](MOYASAR_TOPUP_FLOW.md).
This guide describes the Python code as reviewed on **2026-11-08**.

## 1. What is the worker?

The worker is a separate Python program that keeps checking PostgreSQL for saved
webhook events. Starting FastAPI does not automatically start this worker. The mobile does not
call the worker: it calls API endpoints.

```bash
DEBUG=false .venv/bin/python -m app.workers.moyasar_worker
```

```text
API process                              Worker process
    |                                        |
Receive Moyasar webhook                 Read a due event
    |                                        |
Verify secret                           Fetch payment from Moyasar
    |                                        |
Save event in PostgreSQL --------------> Verify and process the TopUp
    |                                        |
Return 200                              Commit the result
```

The database connects these two programs. The webhook route does not directly
call the worker. Committed events remain available after the API restarts.
The worker must also be running, or restarted, for processing to continue.
At startup it checks that a sandbox API key is configured; a missing, placeholder,
or live key stops it before any event processing. This is a local configuration
check, not proof that Moyasar accepts the key.

This worker is separate from FastAPI's thread pool. The thread pool executes
synchronous endpoints during requests; our worker processes saved events in its
own process.

## 2. Three phases: claim, fetch, finalize

```text
process_one()
    |
    v
Session 1: claim_next() -> short event lock -> attempts += 1
           next_attempt_at = now + 60 seconds -> COMMIT -> close session
    |
    v
MoyasarClient.get_payment() -> HTTP call with NO open DB session/transaction
    |
    v
Session 2: finish_payment() OR finish_retry()
           lock event -> check claim ownership -> lock TopUp -> apply outcome
           COMMIT -> close session
    |
    v
Log support alert if one was committed
```

`main()` repeats `process_one()`. No job means sleep 2 seconds. An unexpected
exception rolls back the active transaction and the loop sleeps 5 seconds.
The committed claim survives: another worker can reclaim after its lease expires.

`ClaimedPaymentJob` is a frozen dataclass, not a database model. It carries plain
`event_id`, `payment_id`, and `attempt` values after the first session closes.
`PaymentJobRepository` selects/claims work; it does not make HTTP calls.

## 3. What does TopUpPaymentProcessor do?

It finalizes a claimed job using database operations only. The worker owns commits.

```text
Lock event -> already processed or attempt changed? Ignore stale result
    |
Lock expected TopUp -> missing? Retry -> terminal? Finish event without credit
    |
Compare payment ID, amount, currency -> mismatch? REVIEW_REQUIRED + support alert
    |
Payment status
    +-- initiated / authorized -> retry
    +-- failed -> FAILED + support alert; no credit
    +-- other non-paid status -> REVIEW_REQUIRED + support alert
    +-- paid -> lock wallet -> verify owner, currency, ACTIVE
                     |
           Transaction + CREDIT ledger + balance + COMPLETED + event finished
                     |
                 One COMMIT
```

COMPLETED, FAILED and REVIEW_REQUIRED are not automatically reopened by a later
event. A failed payment requires a new funding request for a new payment attempt;
manual review must investigate any conflicting later result.

`httpx.Client` is synchronous and blocks the worker thread while fetching. It does
not hold our database connection or row lock during that HTTP request.

## 4. Which models are involved?

SQLAlchemy models describe database tables. Pydantic schemas describe and validate
input/output data; they do not create tables. An enum defines allowed values.

| Name and kind | What it represents | Where its data comes from | Where we use it |
| --- | --- | --- | --- |
| `CreateTopUpRequest` — Pydantic schema | A request to fund a wallet: `wallet_id` and `amount`. | Mobile JSON sent to `POST /topups`. | FastAPI validates it; the route passes its fields to `TopUpService`. |
| `TopUp` — database model (`topups`) | Nexa's funding request and expected payment details. | `TopUpService` combines validated input, the authenticated user, wallet currency, and generated UUIDs. | Returned to the mobile; later locked and checked by the processor before posting. |
| `TopUpStatus` — enum | Nexa's funding state: PENDING, COMPLETED, FAILED, REVIEW_REQUIRED. | Set by Nexa code; it is separate from Moyasar's payment status. | Stored on `TopUp`, checked by the processor, and returned in `TopUpResponse`. Confirmed matching failed payments set FAILED. |
| `TopUpResponse` — Pydantic schema | The public details and current status of a TopUp. | A saved `TopUp` ORM object through `from_attributes=True`. | Responses from `POST /topups` and owner-scoped `GET /topups/{id}`. |
| `MoyasarWebhookEvent` — Pydantic schema | The incoming notification envelope, including its secret and payment data. | Moyasar sends JSON to `POST /webhooks/moyasar`. | Validates incoming fields; the route separately authenticates the secret and selects data to save. |
| `MoyasarWebhookEventRecord` — database model (`moyasar_webhook_events`) | A durable inbox item with retry and completion information. | The webhook route saves authenticated notifications; reconciliation creates local recovery events. | The job repository claims records; the processor schedules retries, finishes them and persists support alerts. |
| `MoyasarPayment` — Pydantic schema | Payment details fetched independently: ID, status, amount, currency. | JSON returned by `GET /v1/payments/{id}` through `MoyasarClient`. | The processor compares it with the saved TopUp before crediting a wallet. |

These existing database models are reused for funding:

| Model | Source and use in a TopUp |
| --- | --- |
| `User` | Existing authenticated account; its ID identifies the owner of the request. |
| `Wallet` | Existing wallet selected by the user. The processor locks it, checks ownership/currency/ACTIVE status, and updates its balance. |
| `Transaction` | Created by the processor for the financial operation, with `sender_wallet_id=None` for external funding. Used by transaction history and linked from TopUp. |
| `LedgerEntry` | Created by the processor with `direction=CREDIT` and `entry_type=TOP_UP`. Records the wallet increase and appears in ledger history. |

`LedgerDirection` describes the balance effect; `LedgerEntryType` describes the
business operation. `TransactionStatus.SUCCESS` describes the financial record.

## 5. Keep the IDs separate

| ID | Created by | Purpose |
| --- | --- | --- |
| `TopUp.id` | Nexa | Mobile checks this funding request using `GET /topups/{id}`. |
| `TopUp.moyasar_payment_id` | Nexa, passed to the SDK as `givenID` | Expected payment ID; connects the external payment to the saved request. |
| Webhook `id` / record `id` | Moyasar | Identifies one notification; the primary key prevents saving that event twice. |
| Webhook `data["id"]` / record `payment_id` | Received from Moyasar | Identifies the payment to fetch; must match the expected payment ID. |
| `Transaction.id` | Nexa database when the INSERT is flushed | Connects the financial transaction, ledger entry, and completed TopUp. |

```text
Mobile request -> TopUp -> TopUpResponse -> Mobile SDK payment
                    ↑
Incoming webhook -> MoyasarWebhookEvent -> MoyasarWebhookEventRecord
                    |                              |
                    |                         Worker selects it
                    |                              ↓
                    +---- compare against ---- MoyasarPayment (API lookup)
                                                   |
                                                   ↓
                                    Transaction + LedgerEntry + Wallet update
```

## 6. Why lock three records?

| Lock | What it protects |
| --- | --- |
| Event row, with `skip_locked=True` | Two workers cannot select the same event together. A worker skips busy events and looks for another. |
| TopUp row | Different events for the same payment cannot both credit it. After waiting, the second worker sees COMPLETED and skips posting. |
| Wallet row | Concurrent operations cannot overwrite each other's balance changes. |

Each lock lasts only until its own transaction ends. The initial claim lock is
released BEFORE HTTP. Finalization locks the event again and checks the attempt
number, then locks the TopUp and wallet as needed.

## 7. Example: 50 AED balance + 100 AED funding

The processor converts `amount_minor=10000` to `Decimal("100")` and prepares:

```text
Transaction: SUCCESS, sender=None, receiver=Wallet A, amount=100
LedgerEntry: Wallet A, TOP_UP, CREDIT, amount=100
Wallet A:    50 -> 150 AED
TopUp:       COMPLETED, linked to the new transaction
Event:       processed_at set
```

All changes commit together when the worker's transaction block exits normally.
If an exception escapes before commit, the changes roll back and the balance
remains 50 AED. `flush()` sends SQL and obtains `transaction.id`; it does not make
the changes permanent. Repeated notifications must leave the balance at 150 AED.

## 8. Waiting for payment versus technical retries

`finish_retry(..., waiting_for_payment=True)` handles 404, initiated and authorized.
These mean we are waiting, not that the payment failed:

- TopUp younger than 15 minutes AND claim count below 8: check again in 2 minutes.
- Older TopUp OR claim count at least 8: check again in 60 minutes.
- Keep PENDING and the same unfinished event, without an attempt cutoff for waiting.

A new paid webhook can be processed immediately while the older event is scheduled
for later. Both use the same locked posting logic, so credit happens once. If that
webhook is lost, the scheduled event can still discover the late payment. There is
currently no automatic expiry for this slow polling.

Technical errors (timeouts, non-404 HTTP errors, malformed responses) use
exponential backoff: 2, 4, 8, 16, 32, then 60 minutes. At claim count 8 a support
alert is persisted once, but the TopUp stays PENDING and hourly checks continue.
Time and retry count alone never mean FAILED or REVIEW_REQUIRED. The counter
counts all claims and is never reset because it also fences stale workers.
A later paid response can still complete the TopUp; support alerts remain historical.

`processed_at` means the job ended, not necessarily that money was credited.
Read `TopUp.status` for the funding result and `support_alert_at` for support needs.

## 9. Polling: how does the worker find work?

Polling means asking repeatedly whether work is ready. There are two different
polling flows in Nexa:

| Caller | Target | Question |
| --- | --- | --- |
| Worker | PostgreSQL | Is there an unfinished event whose retry time is due? |
| Mobile | `GET /topups/{id}` | Has my wallet funding completed yet? |

The worker waits 2 seconds only when no event is ready. After handling an event,
it immediately checks for more work.

| Event ID | Payment ID | processed_at | next_attempt_at | Eligible at 15:05? |
| --- | --- | --- | --- | --- |
| E100 | P123 | NULL | 15:10 | No: retry time has not arrived. |
| E101 | P456 | 14:30 | 14:00 | No: processing already ended. |
| E102 | P789 | NULL | 15:00 | Yes, unless another worker has locked it. |

These short IDs are illustrative; actual payment IDs are UUIDs.

Conceptually, the selection query is:

```sql
SELECT *
FROM moyasar_webhook_events
WHERE processed_at IS NULL
  AND next_attempt_at <= NOW()
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

| SQLAlchemy expression | Meaning |
| --- | --- |
| `select(MoyasarWebhookEventRecord)` | Select records from the event table. |
| `.where(...)` | Apply both unfinished and due-time conditions with AND. |
| `.order_by(...created_at)` | Prefer the oldest eligible event. |
| `.limit(1)` | Process one event per worker iteration. |
| `.with_for_update(skip_locked=True)` | Lock one event and skip events held by another worker. |
| `db.scalar(...)` | Execute the query and return the first selected ORM object, or None. |

```text
Worker A locks E100       Worker B sees E100 locked
        |                            |
    Saves lease                Skips E100
        |                            |
  COMMIT / ROLLBACK        Tries another due event
        |
  Row lock released
```

A row lock prevents conflicting writes/locks; it does not block every ordinary
SELECT. Locks end when the transaction commits or rolls back. If the worker
process crashes, PostgreSQL releases its locks when the connection/transaction ends.

## 10. Lease, retry and stale worker example

```text
15:10  Worker A claims E100, attempt=1, next_attempt_at=15:11; commits
15:10  A calls Moyasar without DB locks
15:11  A is stalled; Worker B can reclaim E100, attempt=2; commits
15:11  A returns late -> attempt 1 != stored 2 -> ignore A's result
15:11  B returns -> attempt 2 matches -> finalize safely
```

The lease is a timestamp, not a database lock. If no one reclaims the event, its
current attempt can still finalize after 60 seconds. The attempt comparison
prevents an old owner from overwriting a newer owner.

```python
# In claim_next():
event.attempts += 1
event.next_attempt_at = now + timedelta(seconds=60)

# In finish_retry(), only while attempts < 8:
delay = min(2 ** event.attempts, 60)
event.next_attempt_at = utc_now() + timedelta(minutes=delay)
```

`**` means exponentiation; `min` caps the delay at 60 minutes. The attempt cap is
checked before calculating the delay. This is exponential backoff. It is Nexa's
retry policy, separate from Moyasar's webhook delivery retries.

## 11. Timeline and crash recovery

```text
15:00  Webhook saved and committed -> return 200
15:00  Payment lookup times out
       attempts = 1; next_attempt_at = 15:02 -> commit retry schedule
15:01  Worker can process other events; this event is not due
15:02+ Worker picks this event when available and verifies paid/amount/currency
       Credit wallet and complete TopUp/event in one commit
Later  Mobile reads COMPLETED and refreshes wallet balance
```

The retry time is the earliest eligible time, not a guarantee of exact execution.
A busy or stopped worker may process it later.

- **Crash before commit:** unfinished changes roll back when the transaction ends;
  the saved event remains available for a later attempt.
- **Crash after commit:** the event is processed and the TopUp is COMPLETED. A
  duplicate event for the same payment must not credit the wallet again.

Transactions, locks, and idempotency work together: all-or-nothing posting,
coordination between workers, and no repeated credit for the same payment.

## 12. Failed payments and support visibility

| Outcome | TopUp | Event | Support |
| --- | --- | --- | --- |
| Matching paid payment | COMPLETED | Finished | No alert |
| Matching failed payment | FAILED | Finished; no automatic retry | Durable alert |
| ID/amount/currency mismatch or invalid wallet | REVIEW_REQUIRED | Finished | Durable alert |
| 404 / initiated / authorized | PENDING | Recheck in 2 or 60 minutes based on TopUp age | Reason saved |
| Technical error below claim limit | PENDING | Exponential backoff | Reason saved |
| Technical error at slow-check threshold | PENDING | Hourly retry, unfinished | Durable alert once |

The webhook accepts `payment_paid` and `payment_failed`. Both trigger an independent
provider lookup. Reconciliation can also discover a failed payment without a webhook.
Subscribe to `payment_failed` in Moyasar for prompt failure notifications.

`support_alert_at` and a safe `last_error` are stored on the event in the same
transaction as FAILED/REVIEW_REQUIRED. The worker emits `SUPPORT_ALERT` only after
commit. Alerts survive a crash between commit and logging because the database is
the durable source. No card data, API key, webhook secret, or raw provider body is stored.

```bash
DEBUG=false .venv/bin/python scripts/list_payment_support_alerts.py --limit 50
```

This is a local, read-only support view, not a public admin API. Alerts are historical
records; acknowledgement/resolution UI and external notification delivery are not
implemented. No email or Slack message is sent by this change.

Migration `b71e58d42a90` adds nullable `support_alert_at`; no new table or enum needed.
Restart all old Moyasar worker processes before testing the new code: Python workers
do not automatically reload, and the old worker has different attempt semantics.

```bash
DEBUG=false .venv/bin/python -m app.workers.moyasar_worker
```

## 13. Validation

75 automated tests passed, including late-payment recovery, bounded fast retries, failed-payment alerts,
lease recovery, stale-owner rejection, rollback and no DB connection during HTTP.
Isolated PostgreSQL tests also verified concurrent credit, simultaneous claims,
stale workers, and acquiring event locks from another connection during lookup.
Provider responses were simulated; existing application balances were untouched.

```bash
DEBUG=false .venv/bin/python -m unittest discover -s tests -v
DEBUG=false .venv/bin/python scripts/test_topup_concurrency.py --run-local
```

Provider status meanings: [Moyasar Payment Status Reference](https://docs.moyasar.com/api/payments/payment-status-reference).
