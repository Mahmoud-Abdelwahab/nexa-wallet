# Queue terminology in this flow:
# Producer = Reconciliation Worker: inserts recovery events for eligible TopUps.
# Queue = moyasar_webhook_events: database table holding events for processing.
# Consumer = Moyasar Worker: reads due events and processes them through the payment processor.
# The webhook endpoint is also a producer: it inserts events received from Moyasar.
# Producer -> Queue -> Consumer

# Business: recover missing processing work for old PENDING TopUps.
# This service ONLY inserts missing reconciliation events into moyasar_webhook_events.
# That table is our database queue; there is no second queue to push into.
# It does not call Moyasar, change TopUp status, or credit a wallet.
# The existing moyasar_worker reads these events and uses TopUpPaymentProcessor
# to fetch the payment, verify it, and credit once when all checks pass.
# The separate reconciliation worker calls this service, then sleeps 60 seconds.

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.topup import TopUp, TopUpStatus
from app.models.moyasar_webhook_event import (
    MoyasarWebhookEventRecord,
)


class TopUpReconciliationService:

    def __init__(self, db: Session):
        self.db = db

    # enqueue_pending_topups()
    #           |
    #           v
    # 1. Set cutoff = now (UTC) minus 5 minutes
    #           |
    #           v
    # 2. Build pending_event_exists for each candidate TopUp's Payment ID
    #           |
    #           v
    # 3. Select up to 100 oldest TopUps matching ALL conditions:
    #    PENDING + created at/before cutoff + NO unfinished event
    #           |
    #           v
    # 4. For each TopUp, build a stable Event ID: reconcile:{topup.id}
    #           |
    #           v
    # 5. INSERT event_type=reconciliation, processed_at=NULL (default)
    #           +-- Event ID already exists -> skip INSERT
    #           +-- New Event ID -> add one row to the database queue
    #           |
    #           v
    # 6. Return inserted count; the calling worker commits the transaction
    #           |
    #           v
    # Existing moyasar_worker -> TopUpPaymentProcessor -> verify payment
    #                         -> credit once / retry / review as appropriate
    #
    # Example: all three TopUps are PENDING and at least 5 minutes old.
    #
    # topups                         moyasar_webhook_events
    # TopUp ID | Payment ID          Event ID | Payment ID | processed_at
    # T100     | P100                E500     | P100       | NULL
    # T101     | P101                (no event for P101)
    # T102     | P102                E501     | P102       | NULL
    #
    # P100 -> unfinished event exists -> skip
    # P101 -> no unfinished event     -> insert reconcile:T101
    # P102 -> unfinished event exists -> skip
    #
    # New row: id=reconcile:T101, payment_id=P101,
    #          event_type=reconciliation, processed_at=NULL.
    # T101 stays PENDING: event_type is a queue label, not a TopUp status.
    # T100/T101/T102 are teaching labels; real TopUp IDs are UUIDs.
    def enqueue_pending_topups(self) -> int:
        # 1. Allow normal webhook delivery some time; age alone does not prove it was lost.
        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=5
        )

        # 2. EXISTS asks: is there ANY unfinished event for this TopUp's Payment ID?
        # The outer SELECT below supplies the current TopUp; this is not executed yet.
        # processed_at IS NULL means unfinished, including events scheduled for retry.
        # It does not matter whether the event came from Moyasar or reconciliation.
        pending_event_exists = (
            select(MoyasarWebhookEventRecord.id)
            .where(
                MoyasarWebhookEventRecord.payment_id
                == TopUp.moyasar_payment_id,
                MoyasarWebhookEventRecord.processed_at.is_(None),
            )
            .exists()
        )

        # 3. All WHERE conditions use AND. ~ negates EXISTS: require NO such event.
        # A processed event does not satisfy pending_event_exists.
        stmt = (
            select(TopUp)
            .where(
                TopUp.status == TopUpStatus.PENDING,
                TopUp.created_at <= cutoff,
                ~pending_event_exists,
            )
            .order_by(TopUp.created_at)
            .limit(100)
        )

        # Execute the query and collect the selected TopUp objects.
        topups = self.db.scalars(stmt).all()

        enqueued = 0

        for topup in topups:
            # 4. Stable ID: later scans generate the same value, not a fresh UUID.
            # Even a processed reconcile:{id} record blocks inserting that ID again.
            event_id = f"reconcile:{topup.id}"

            # 5. This is a locally generated recovery event, not a received webhook.
            # The primary key handles duplicate reconciliation inserts, including races.
            # A real webhook may still arrive concurrently; posting logic prevents double credit.
            statement = (
                insert(MoyasarWebhookEventRecord)
                .values(
                    id=event_id,
                    payment_id=topup.moyasar_payment_id,
                    event_type="reconciliation",
                )
                .on_conflict_do_nothing(
                    index_elements=["id"]
                )
                .returning(MoyasarWebhookEventRecord.id)
            )

            # INSERT returns the new ID; a duplicate returns None and is not counted.
            inserted_id = self.db.scalar(statement)
            if inserted_id is not None:
                enqueued += 1

        # 6. Do not commit here; the worker owns commit/rollback for the whole batch.
        return enqueued
