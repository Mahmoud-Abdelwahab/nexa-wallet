# Moyasar Worker and TopUp Models — Learning Guide

Read this alongside the [six-step business flow](MOYASAR_TOPUP_FLOW.md).
This guide describes the Python code as reviewed on **2026-11-07**.

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

## 2. How does the loop work?

```python
while True:
    with SessionLocal.begin() as db:
        processor = TopUpPaymentProcessor(db)
        processed = processor.process_next()
```

| Code | Meaning |
| --- | --- |
| `while True` | Repeat until the program is stopped. |
| `SessionLocal.begin()` | Open a session and transaction for this iteration. Normal exit commits; an escaping exception rolls back. The session closes afterward. |
| `TopUpPaymentProcessor(db)` | Create a processor that uses that same session. Creating the object does not process a payment. |
| `process_next()` | Try to handle one due event. |
| `False` | No event was available; the worker sleeps for 2 seconds. |
| `True` | An event was handled, including a retry or review decision. It does not necessarily mean a wallet was credited. |

An unhandled exception reaches the worker after rollback. The worker logs it,
sleeps for 5 seconds, then starts another iteration.
The `if __name__ == "__main__"` block starts the loop when the module is launched
as a program; importing the module alone does not start it.

## 3. What does TopUpPaymentProcessor do?

The worker manages repetition and transaction lifetime. The processor implements
the funding rules for one event.

```text
Find an unfinished event whose next_attempt_at is due
    ↓
Lock event; skip events already locked by another worker
    ↓
Fetch payment using MoyasarClient
    ↓
Lock the expected TopUp using the event Payment ID; check the fetched ID
    ↓
Already COMPLETED or REVIEW_REQUIRED? -> finish event without another credit
    ↓
Check payment status, amount, and currency
    ├── Waiting for payment -> schedule retry
    ├── Payment ID/amount/currency mismatch -> REVIEW_REQUIRED, no credit
    └── Valid -> lock wallet and check owner/currency/ACTIVE
                     ↓
          Create Transaction and TOP_UP / CREDIT LedgerEntry
                     ↓
          Update balance; link and complete TopUp; finish event
                     ↓
          Return to worker -> COMMIT
```

`MoyasarClient` performs the external HTTP request using `httpx.Client`.
The synchronous call waits for the response in the worker's thread. It does not
start another worker automatically. The event lock remains held during the lookup.

## 4. Which models are involved?

SQLAlchemy models describe database tables. Pydantic schemas describe and validate
input/output data; they do not create tables. An enum defines allowed values.

| Name and kind | What it represents | Where its data comes from | Where we use it |
| --- | --- | --- | --- |
| `CreateTopUpRequest` — Pydantic schema | A request to fund a wallet: `wallet_id` and `amount`. | Mobile JSON sent to `POST /topups`. | FastAPI validates it; the route passes its fields to `TopUpService`. |
| `TopUp` — database model (`topups`) | Nexa's funding request and expected payment details. | `TopUpService` combines validated input, the authenticated user, wallet currency, and generated UUIDs. | Returned to the mobile; later locked and checked by the processor before posting. |
| `TopUpStatus` — enum | Nexa's funding state: PENDING, COMPLETED, FAILED, REVIEW_REQUIRED. | Set by Nexa code; it is separate from Moyasar's payment status. | Stored on `TopUp`, checked by the processor, and returned in `TopUpResponse`. The current processor does not assign FAILED. |
| `TopUpResponse` — Pydantic schema | The public details and current status of a TopUp. | A saved `TopUp` ORM object through `from_attributes=True`. | Responses from `POST /topups` and owner-scoped `GET /topups/{id}`. |
| `MoyasarWebhookEvent` — Pydantic schema | The incoming notification envelope, including its secret and payment data. | Moyasar sends JSON to `POST /webhooks/moyasar`. | Validates incoming fields; the route separately authenticates the secret and selects data to save. |
| `MoyasarWebhookEventRecord` — database model (`moyasar_webhook_events`) | A durable inbox item with retry and completion information. | The webhook route saves Event ID, Payment ID, and event type after authentication. | The processor selects due records, schedules retries, and marks events processed. |
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

The locks last until the database transaction commits or rolls back.

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

## 8. Retry scheduling versus sleeping

`_retry()` updates the event's counter, reason, and `next_attempt_at`. It does not
sleep. Delays are 2, 4, 8, 16, 32, then 60 minutes per later retry. The worker
commits that schedule and can process other events while this one waits.

The 60-minute limit caps the delay, not the number of attempts. An unhandled
exception instead rolls back; the worker sleeps 5 seconds, and no retry metadata
is saved for that failure in the current code.

`processed_at` means processing ended, including review/unknown-payment outcomes;
it does not prove the wallet was credited. Read `TopUp.status` for funding status.

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
    Processes it                Skips E100
        |                            |
  COMMIT / ROLLBACK        Tries another due event
        |
  Row lock released
```

A row lock prevents conflicting writes/locks; it does not block every ordinary
SELECT. Locks end when the transaction commits or rolls back. If the worker
process crashes, PostgreSQL releases its locks when the connection/transaction ends.

## 10. Understand the retry expression

```python
event.attempts += 1
delay = min(2 ** min(event.attempts, 6), 60)
event.next_attempt_at = utc_now() + timedelta(minutes=delay)
event.last_error = reason[:255]
```

- `+= 1` increases the scheduled-retry counter.
- `**` means exponentiation: `2 ** 1 = 2`, `2 ** 2 = 4`, `2 ** 3 = 8`.
- The inner `min(..., 6)` prevents calculating an unnecessarily large exponent.
- The outer `min(..., 60)` caps the delay at 60 minutes.
- `timedelta(minutes=delay)` adds that duration to the current time.
- `reason[:255]` keeps the stored reason within the column length.

This increasing delay is called **exponential backoff**. These are Nexa's choices,
not Moyasar's webhook delivery schedule. There is no separate timer reserved for
each event: its next eligible time is simply stored in PostgreSQL.

A lookup timeout means we could not confirm payment status. It does not prove
that the payment failed. Retrying a lookup does not create another payment.

| Retry type | Who retries? | Why? |
| --- | --- | --- |
| Webhook delivery | Moyasar | Nexa did not successfully acknowledge delivery/storage. |
| Payment processing | Nexa worker | The saved event needs another lookup, for example after a network or invalid-response error. |

These retries are independent. The current MVP ignores unsupported webhook types
with an explicit `ignored=true` acknowledgement rather than saving them.

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

## 12. Fixes and remaining improvements

Implemented and tested:

- A wallet that became INACTIVE or SUSPENDED is marked for review without credit.
- Invalid JSON or payment fields raise a safe `MoyasarInvalidResponseError` and
  schedule a retry, allowing later events to run. Money fields use strict types.
- A mismatched Payment ID reviews the TopUp identified by the event's expected
  ID, never an unrelated TopUp selected by the unexpected response ID.
- `_mark_for_review()` records the reason and finishes the event without credit;
  it preserves COMPLETED on an already credited TopUp.
- Worker startup rejects placeholder API keys. Put your real test key in `.env`;
  the code cannot generate a valid Moyasar API credential.

Still deferred:

- The worker holds the event row lock during the HTTP lookup. Releasing it safely
  requires a separate claim/lease design with crash recovery; simply removing the
  lock would weaken coordination.
- Retry counts have no terminal limit or operational alert yet. Persistent HTTP
  failures and unrelated unexpected processing failures need further handling.
- Real SDK/payment integration must still be checked with a valid Moyasar test key.

Validation: 48 automated tests passed. A separate isolated PostgreSQL test ran two
workers on distinct events for the same payment and produced one transaction,
one credit entry, and a 50 -> 150 AED balance. Moyasar responses were simulated;
existing application balances were untouched.

```bash
DEBUG=false .venv/bin/python -m unittest discover -s tests -v
DEBUG=false .venv/bin/python scripts/test_topup_concurrency.py --run-local
```
