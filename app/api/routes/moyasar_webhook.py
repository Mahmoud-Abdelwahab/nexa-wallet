import logging
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import get_db
from app.models.moyasar_webhook_event import MoyasarWebhookEventRecord
from app.schemas.moyasar_webhook import MoyasarWebhookEvent


router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
logger = logging.getLogger(__name__)


# Business — Step 3: save the notification before acknowledging receipt.
# POST /webhooks/moyasar
#     ↓
# Verify secret -> reject live events
#     ↓
# webhook_verification? -> show the setup code in the server terminal -> 200
#     ↓ No
# payment_paid or payment_failed?
#     ├── No  -> 200 (ignored; no event saved)
#     └── Yes -> validate Payment ID
#                    ↓
#              INSERT event (duplicate Event ID -> skip)
#                    ↓
#                  COMMIT -> 200 (safely received)
#                    └── DB failure -> ROLLBACK -> error, not 200
# A separate worker will verify payment and credit once; this route only saves it.
# Use def with our sync Session so FastAPI runs the endpoint in its thread pool.
@router.post("/moyasar", status_code=status.HTTP_200_OK)
def receive_moyasar_webhook(
    event: MoyasarWebhookEvent,
    db: Session = Depends(get_db),
):
    # 1. Reject requests if the server secret is missing or empty.
    configured_secret = settings.MOYASAR_WEBHOOK_SECRET
    if configured_secret is None or not configured_secret.get_secret_value():
        raise HTTPException(503, "Webhook is not configured")

    # 2. Unwrap SecretStr; compare bytes to also handle non-ASCII input safely.
    # compare_digest avoids content-based early exits; never log either secret.
    if not secrets.compare_digest(
        event.secret_token.encode("utf-8"),
        configured_secret.get_secret_value().encode("utf-8"),
    ):
        raise HTTPException(403, "Invalid webhook secret")

    # 3. Sandbox only: live payments are not supported yet.
    if event.live:
        raise HTTPException(403, "Live payments not supported")

    # Setup only: enter this short-lived code in the Moyasar dashboard.
    # Log only the authenticated sandbox code, never the secret or full payload.
    # This event is not a payment and must never reach the financial worker.
    if event.type == "webhook_verification":
        code = event.data.get("verification_code")
        if isinstance(code, int) and not isinstance(code, bool):
            code = str(code)
        if (
            not isinstance(code, str)
            or len(code) != 6
            or not code.isascii()
            or not code.isdigit()
        ):
            raise HTTPException(422, "Invalid webhook verification code")
        logger.info("Moyasar webhook verification code: %s", code)
        return {"received": True}

    # 4. Queue paid/failed notifications; verify the actual status with Moyasar first.
    # The webhook payload alone cannot authorize credit or declare a payment failed.
    if event.type not in ("payment_paid", "payment_failed"):
        return {"received": True, "ignored": True}

    # 5. Validate and normalize the Payment ID before saving it for the worker.
    # UUID expects a string here; reject lists, numbers, and objects with 422.
    raw_payment_id = event.data.get("id")
    if not isinstance(raw_payment_id, str):
        raise HTTPException(422, "Invalid payment ID")
    try:
        payment_id = str(UUID(raw_payment_id))
    except ValueError:
        raise HTTPException(422, "Invalid payment ID")

    # 6. The Event ID primary key deduplicates notifications, even concurrently.
    # Store only processing metadata, not the secret or card details.
    stmt = (
        insert(MoyasarWebhookEventRecord)
        .values(id=event.id, payment_id=payment_id, event_type=event.type)
        .on_conflict_do_nothing(index_elements=["id"])
    )

    # 7. Acknowledge only after durable commit; failures must remain retryable.
    try:
        db.execute(stmt)
        db.commit()
    except Exception:
        db.rollback()
        raise

    # Received means saved (or already saved), not that the wallet was credited.
    return {"received": True}
