# TopUp verification — 2026-11-07

## Existing sandbox payment

Read-only checks against Moyasar and the local database:

| Record | Observed result |
| --- | --- |
| Payment | `84f7766c-2503-45d8-bb55-fe28280ced70`, `paid`, 300 minor units, AED |
| TopUp | `b62a012b-8d59-432a-9765-540072a6f65c`, `COMPLETED`, matching payment/amount/currency |
| Transaction | ID 4, `SUCCESS`, 3 AED, no internal sender, receiver wallet 1 |
| Ledger | One entry: ID 7, `TOP_UP / CREDIT`, 3 AED, linked to transaction 4 |
| Webhook | One saved event, processed, zero scheduled retries, no recorded error |
| Wallet 1 | Current balance at inspection: 353.0000 AED |

No new provider payment was created and no existing wallet balance was changed by these checks.

## Automated checks

- 53 unit tests passed, including payment ID lifetime, ownership, duplicate events,
  mismatched payment data, inactive wallets, retry scheduling, and rollback.
- The isolated PostgreSQL concurrency test passed: two workers handling different
  events for the same payment produced one transaction, one credit, and a balance
  change from 50 to 150. The provider was mocked and the temporary schema removed.
- These checks do not constitute a full production readiness review.

```bash
DEBUG=false .venv/bin/python -m unittest discover -s tests -v
DEBUG=false .venv/bin/python scripts/test_topup_concurrency.py --run-local
```

## Payment ID lifetime

```text
POST /topups + key A -> new TopUp A -> generate Payment ID A once
                                  |
                     GET A / worker retry / duplicate webhook
                                  |
                          keep Payment ID A

POST /topups + key A + same body -> return TopUp A and Payment ID A
POST /topups + key A + changed body -> 409
POST /topups + key B -> new TopUp B -> generate Payment ID B
```

The service generates the ID with `uuid4()` and stores it. Reads return the saved
value; completion and retries do not regenerate it. The mobile passes this value
as `givenID` (the payment API JSON field is `given_id`).

**Update:** creation idempotency is now implemented. New POST requests require
`Idempotency-Key`; the same user's key and equivalent wallet/amount return the
saved request. A different wallet or amount returns 409. New keys create new
requests. Existing records without a key remain readable via GET.

60 unit tests now pass. Additional isolated PostgreSQL checks passed for three
concurrent identical creations (one TopUp, one Payment ID), and concurrent
different amounts sharing a key (one winner, one conflict). No credit was posted
by these creation tests. Migration `a903bc521d40` was applied locally.

## Duplicate webhook test against the running local API and worker

On 2026-11-07, replayed the completed sandbox payment above using authenticated
POST requests to `http://127.0.0.1:8000/webhooks/moyasar`:

1. Sent the original Event ID twice. Both responses were HTTP 200. The inbox kept
   one record for that ID and its original processing timestamp was unchanged.
2. Sent a new test Event ID for the same Payment ID. The API returned HTTP 200,
   and the running worker processed it without errors or scheduled retries.
   The completed TopUp prevented a second financial posting.

| Financial state | Before | After both cases |
| --- | --- | --- |
| Wallet 1 balance | 353.0000 AED | 353.0000 AED |
| TopUp status | COMPLETED | COMPLETED |
| Linked transaction | 4 | 4 |
| Transactions with receiver wallet 1 | 1 | 1 |
| Ledger entries for this TopUp transaction | 1 | 1 |
| All wallet 1 ledger entries | 2 | 2 |

The payment ID also remained unchanged. No new payment was initiated.
The intentionally created test inbox record remains for traceability:
`duplicate-test-b8351823f852467eb1328ee9de5251b8`, processed at
`2026-11-07 19:02:12.389420+00:00`.

## 2026-11-08 — Claim/fetch/finalize validation

69 tests passed. Added cases cover 404 retry timing/exhaustion, failed-payment
support alerts, committed leases, stale-worker rejection, finalization rollback,
and no checked-out database connection during provider HTTP. PostgreSQL checks
confirmed event locks are released during lookup, only one worker claims an event,
and expired-lease recovery cannot double-credit. New migration b71e58d42a90 was
applied; support-alert viewer reads successfully. All financial test fixtures were
isolated and provider responses mocked. Old running workers need a restart.

## Late-payment policy update — 2026-11-08

72 tests passed. 404 and in-progress payments remain PENDING with slower checks
after 15 minutes; late paid results still credit after more than eight claims.
Verified late-webhook and no-webhook recovery without a second credit. Technical
errors retain the claim-count limit. No schema changes or existing data edits.

### 2026-11-08 — Reconciliation scenario checks

- Added producer-to-consumer tests starting with an old TopUp and no webhook.
- 404: stays PENDING, same event rechecked later; slow checks by age/claim threshold.
- Timeout/5xx: exponential backoff capped at 60 minutes. Claim 8 records a support
  alert once while keeping PENDING and continuing hourly checks, not REVIEW_REQUIRED.
- Paid: verify and credit once, including a later duplicate webhook.
- 75 tests passed using isolated SQLite data and mocked provider responses. No
  new migration or existing-data changes. Restart the consumer to load this policy.
