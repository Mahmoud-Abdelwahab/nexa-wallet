# Nexa Wallet — TopUp Reconciliation

Status: updated on 2026-11-08. Reconciliation enqueues recovery events; the existing
Moyasar worker performs provider lookups and financial posting.

## Why do we need it?

Reconciliation means comparing Nexa's funding records with the payment provider
and safely resolving differences.

A customer can pay successfully while the webhook never reaches Nexa. Our current
worker reads saved webhook events; without an event, it has nothing to process.
Retries of saved events cannot recover an event that was never saved.

```text
Customer pays 50 AED
         |
         v
Moyasar: paid
         |
         X  Webhook never reaches Nexa
         |
Nexa: TopUp remains PENDING
Wallet: no credit yet
```

The reconciliation worker provides a second way to discover this payment. It
checks existing payments; it does not initiate payments or charge the customer.

## Implemented flow

```text
Reconciliation worker (Producer): scan, then sleep 60 seconds
      |
      v
Find up to 100 oldest PENDING TopUps at least 5 minutes old
      |
      +-- Unfinished event already exists for Payment ID? Skip
      |
      v
INSERT reconcile:{topup.id}, event_type=reconciliation
ON CONFLICT(id) DO NOTHING -> COMMIT
      |
      v
moyasar_webhook_events (database Queue)
      |
      v
Moyasar worker (Consumer)
      |
      +-- Claim/60-second lease -> COMMIT, release lock
      +-- Fetch payment outside DB transaction
      +-- Finalize with locks + attempt check -> COMMIT
```

The producer does not call Moyasar, change TopUp status or credit money.
`pending_event_exists` includes retrying and leased events because their
`processed_at` remains NULL. The stable reconciliation ID also prevents inserting
that same recovery event again after it has been processed.

The consumer rechecks 404 and in-progress payments on the same event: every two
minutes initially, then hourly once the TopUp is 15 minutes old, keeping PENDING.
Technical errors use backoff, then hourly checks with one support alert at claim
count eight; the TopUp remains PENDING. Waiting checks also slow down at count eight. A matching failed payment becomes FAILED with an alert immediately.

## What if the payment does not exist?

```text
POST /topups -> Nexa saves PENDING + generates Payment ID
      |
      X  User closes the app before creating the Moyasar payment
      |
Reconciliation looks up that ID -> no payment exists yet
```

A generated Payment ID does not prove that Moyasar has created a payment.
Do not credit the wallet or immediately label the TopUp FAILED just because a
lookup returns not found. Keep it pending initially and retry according to policy.
Waiting alone never marks the TopUp for review; old requests are checked hourly. Authentication errors also need attention, not classification
as "payment not found".

## One shared financial posting path

```text
Saved webhook -> Webhook worker --------+
                                       |
Old PENDING TopUp -> Reconciliation ----+--> Shared verify-and-credit logic
                                              |
                                              v
                                     Credit this TopUp only once
```

Both producers feed the same consumer and `TopUpPaymentProcessor`. Financial
posting is not duplicated in the reconciliation producer.

Different events may refer to the same payment and be consumed simultaneously. The TopUp row lock
and a state check inside the database transaction make the second worker see
COMPLETED and skip the credit. Checking PENDING only when selecting the batch is
not enough. FAILED and REVIEW_REQUIRED must never be automatically reopened by reconciliation.

## Three different protections

| Protection | Problem it solves |
| --- | --- |
| TopUp creation idempotency | A repeated POST must not create another funding request. |
| Payment posting idempotency | Repeated events or workers must not credit one payment twice. |
| Reconciliation | A paid payment must still be discovered when its webhook is missing. |

## Acceptance checks before calling it complete

- Paid 50 AED, matching PENDING TopUp, no webhook: one 50 AED credit and COMPLETED.
- Run reconciliation again: no additional transaction, ledger entry or balance change.
- Webhook and reconciliation run together: one credit only.
- Payment not found or still in progress: no credit; a later check is scheduled.
- Provider outage: no false failure or credit; other eligible requests still progress.
- Mismatched amount/currency/ID or invalid wallet: no credit; review required.
- Posting error: all financial changes roll back together; retry remains possible.

The mobile continues using `GET /topups/{id}`. It does not need to know which
worker completed the funding request.
