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
POST /topups -> new TopUp A -> generate Payment ID A once
                                  |
                     GET A / worker retry / duplicate webhook
                                  |
                          keep Payment ID A

POST /topups again -> new TopUp B -> generate Payment ID B
```

The service generates the ID with `uuid4()` and stores it. Reads return the saved
value; completion and retries do not regenerate it. The mobile passes this value
as `givenID` (the payment API JSON field is `given_id`).

**Current gap:** `POST /topups` has no request idempotency. Repeating the same body
creates another pending TopUp with a different payment ID. Creating a TopUp alone
does not charge or credit money, but clients must not treat repeated creation as
retrieving the previous request. Poll `GET /topups/{id}` for an existing request.
Creation idempotency should be added separately; payment event deduplication
already prevents posting the same payment twice.
