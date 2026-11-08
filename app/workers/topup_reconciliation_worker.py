# Queue terminology in this flow:
# Producer = Reconciliation Worker: inserts recovery events for eligible TopUps.
# Queue = moyasar_webhook_events: database table holding events for processing.
# Consumer = Moyasar Worker: reads due events and processes them through the payment processor.
# The webhook endpoint is also a producer: it inserts events received from Moyasar.
# Producer -> Queue -> Consumer

import logging
import time

from app.database import SessionLocal
from app.services.topup_reconciliation_service import (
    TopUpReconciliationService,
)

logger = logging.getLogger(__name__)

# Business: periodically fill gaps in the database queue; do not credit money here.
# Run separately: python -m app.workers.topup_reconciliation_worker
# FastAPI does not start this worker automatically. Keep moyasar_worker running too.
#
# Reconciliation worker                  Existing Moyasar worker
#          |                                      |
#          v                                      |
# Find old PENDING TopUps                          |
# without an unfinished event                     |
#          |                                      |
#          v                                      v
# INSERT reconciliation events -------> Read due, unfinished events
# into moyasar_webhook_events                     |
#          |                                      v
#          v                              TopUpPaymentProcessor
# COMMIT the batch                                |
#          |                                      v
#          v                              Fetch payment from Moyasar
# Sleep 60 seconds                                |
#          |                                      v
#          +--> repeat                    Verify + credit once / retry / review
#
# Example: T101 / P101 is PENDING, at least 5 minutes old, and has no unfinished event.
# This worker inserts reconcile:T101 and commits; T101 remains PENDING.
# The old worker sees the new row and verifies P101 before any wallet credit.
# On the next scan, an unfinished event makes this worker skip T101.
# A COMPLETED TopUp is also excluded from the scan.
#
# Timing: the first scan runs immediately. Each completed or failed cycle is followed
# by a 60-second sleep, so scan start times are processing duration + 60 seconds apart.
# The 5-minute rule is the TopUp's minimum age, not the worker's sleep interval.
def main():
    # 1. Repeat until the process is stopped.
    while True:
        try:
            # 2. Open one transaction: normal exit commits; an exception rolls back.
            with SessionLocal.begin() as db:
                service = TopUpReconciliationService(db)

                # 3. Enqueue missing work only; payment verification belongs to the old worker.
                count = service.enqueue_pending_topups()

                # 4. Report inserted rows; this log happens BEFORE the context commits.
                logger.info(
                    "Enqueued %s topups for reconciliation",
                    count,
                )

        except Exception:
            # A failed batch rolls back; report the error and try again next cycle.
            logger.exception("Reconciliation failed")

        # 5. Wait 60 seconds after either success or failure, then scan again.
        time.sleep(60)


if __name__ == "__main__":
    main()
