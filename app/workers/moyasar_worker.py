import logging
import time

import httpx

from app.database import SessionLocal
from app.integrations.moyasar_client import MoyasarClient, MoyasarInvalidResponseError
from app.repositories.payment_job_repository import PaymentJobRepository
from app.services.topup_payment_processor import TopUpPaymentProcessor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Consumer: both webhook and reconciliation events use these same three phases.
# 1. Claim -> COMMIT -> close session (release row lock and DB connection)
# 2. GET Moyasar payment (no open DB session/transaction)
# 3. Finalize -> COMMIT -> log any persisted support alert
#
# 404 means payment not found yet, NOT failed: schedule retry on the same event.
# 404 keeps PENDING and slows down with age; it has no attempt-count cutoff.
# Technical errors use backoff, then hourly checks with a support alert at claim 8.
# A confirmed failed payment is terminal; the processor persists FAILED + alert.
# If the process crashes after claiming, the lease makes the event available again.
def process_one(moyasar: MoyasarClient, sessions=SessionLocal) -> bool:
    # Phase 1: reserve one event for 60 seconds and release the database resources.
    with sessions.begin() as db:
        job = PaymentJobRepository(db).claim_next()
    if job is None:
        return False

    payment = None
    reason = None
    waiting_for_payment = False
    # Always fetch: a late payment can now exist, even after many earlier 404s.
    # Phase 2 has no open database transaction.
    try:
        payment = moyasar.get_payment(job.payment_id)
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        waiting_for_payment = code == 404
        reason = "Moyasar payment not found (404)" if code == 404 else f"Moyasar HTTP {code}"
    except httpx.HTTPError:
        reason = "Moyasar network error"
    except MoyasarInvalidResponseError:
        reason = "Invalid Moyasar payment response"

    # Phase 3: short transaction; stale claims cannot change records.
    with sessions.begin() as db:
        processor = TopUpPaymentProcessor(db)
        if reason is not None:
            alert = processor.finish_retry(
                job, reason, waiting_for_payment=waiting_for_payment,
            )
        elif payment is not None:
            alert = processor.finish_payment(job, payment)
        else:
            raise RuntimeError("Payment lookup produced no result")

    # Commit succeeded. The DB alert remains available even if logging/process fails.
    if alert is not None:
        logger.error("SUPPORT_ALERT event_id=%s payment_id=%s reason=%s",
                     alert.event_id, alert.payment_id, alert.reason)
    return True


# Start separately: python -m app.workers.moyasar_worker
# Validate config -> process one job -> no job? Sleep 2 seconds -> repeat.
# Unexpected error -> rollback active phase -> log -> sleep 5 seconds.
# The committed claim survives a crash; another worker can reclaim after 60 seconds.
def main():
    MoyasarClient.validate_configuration()
    moyasar = MoyasarClient()
    while True:
        try:
            if not process_one(moyasar):
                time.sleep(2)
        except Exception:
            logger.exception("Payment worker failed")
            time.sleep(5)


if __name__ == "__main__":
    main()
