import logging
import time

from app.database import SessionLocal
from app.integrations.moyasar_client import MoyasarClient
from app.services.topup_payment_processor import (
    TopUpPaymentProcessor,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Start this worker as a separate program; starting FastAPI does not start it:
# python -m app.workers.moyasar_worker
#
# Start a new loop iteration
#     ↓
# Open a DB session and transaction
#     ↓
# Processor attempts one due event
#     ├── Normal exit -> commit all changes and close session
#     │                    ├── No event? Sleep 2 seconds
#     │                    └── Handled an event? Start the next iteration immediately
#     └── Exception escapes -> rollback and close -> log error and sleep 5 seconds
#     ↓
# Repeat while the program runs
def main():
    # Stop immediately for a missing/placeholder key instead of endlessly retrying events.
    MoyasarClient.validate_configuration()
    # Keep looping until the program is stopped, for example from its terminal.
    while True:
        try:
            # The context manager commits on normal exit and rolls back on an escaping exception.
            # Event, TopUp, transaction, ledger, and balance changes are saved together.
            with SessionLocal.begin() as db:
                # Pass the same session to keep all processor changes in this transaction.
                processor = TopUpPaymentProcessor(db)
                processed = processor.process_next()

            # False means no event is ready; wait 2 seconds to avoid constant empty queries.
            # True means an event was handled, including a scheduled retry or review decision.
            if not processed:
                time.sleep(2)

        except Exception:
            # The transaction has rolled back; log the failure and pause for 5 seconds.
            # An unhandled processor error does not update attempts or next_attempt_at.
            logger.exception("Moyasar worker failed")
            time.sleep(5)


if __name__ == "__main__":
    # Run the loop when launched as a program, not when another module imports this file.
    main()
