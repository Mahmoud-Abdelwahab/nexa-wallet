# Nexa Wallet — TopUp Reconciliation

Status: planned business flow, documented on 2026-11-08. Not implemented yet.

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

## Planned flow

```text
Run periodically
      |
      v
Select a small batch of old PENDING TopUps due for a check
      |
      v
Look up each stored moyasar_payment_id using the backend API key
      |
      +-- Timeout / temporary error -> keep pending; schedule retry
      |
      +-- Payment not found -> no credit; schedule a later check
      |
      +-- Payment still in progress -> no credit; schedule a later check
      |
      +-- Other payment state -> apply an explicit failure/review rule
      |
      +-- Paid -> use shared verification and posting logic
                       |
                       +-- ID / amount / currency mismatch -> REVIEW_REQUIRED
                       |
                       v
                 Lock TopUp; recheck its current state
                       |
                       +-- COMPLETED -> skip; already credited
                       +-- REVIEW_REQUIRED -> skip automatic credit
                       |
                       v
                 Lock wallet; check owner, currency and ACTIVE status
                       |
                       +-- Invalid -> REVIEW_REQUIRED; no credit
                       |
                       v
                 Transaction(SUCCESS) + TOP_UP/CREDIT ledger
                 + increase balance + TopUp(COMPLETED)
                       |
                       v
                 One COMMIT; any posting failure -> ROLLBACK
```

"Old" gives the normal webhook flow time to finish. The age threshold, batch size,
check interval, retry limits and escalation rules must be chosen during implementation.
Store scheduling/progress so the same unpaid requests do not block later ones.

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
Define an abandonment/review policy for long-lived requests; do not retry forever
without visibility. Authentication errors also need attention, not classification
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

Extract the reusable payment verification and posting logic from
`TopUpPaymentProcessor`; do not copy it into a second worker. Keep webhook event
selection/completion separate from reconciliation scheduling.

Both workers may discover the same paid payment simultaneously. The TopUp row lock
and a state check inside the database transaction make the second worker see
COMPLETED and skip the credit. Checking PENDING only when selecting the batch is
not enough. REVIEW_REQUIRED must never be automatically reopened by reconciliation.

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
