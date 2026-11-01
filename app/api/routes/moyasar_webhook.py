import json
import logging
import secrets

from fastapi import APIRouter, HTTPException, status

from app.core.config import settings
from app.schemas.moyasar_webhook import MoyasarWebhookEvent


router = APIRouter(
    prefix="/webhooks",
    tags=["Webhooks"],
)

logger = logging.getLogger(__name__)


@router.post(
    "/moyasar",
    status_code=status.HTTP_200_OK,
)
# Receive event -> verify secret -> reject live mode -> log safe fields -> 200.
# Sandbox connectivity test only; this flow does not save events or credit wallets.
async def receive_moyasar_webhook(
    event: MoyasarWebhookEvent,
):
    # 1. Fail closed if the server has no webhook secret configured.
    configured_secret = settings.MOYASAR_WEBHOOK_SECRET
    if configured_secret is None or not configured_secret.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook is not configured",
        )

    # 2. Unwrap SecretStr only for comparison; never print the secret.
    # compare_digest avoids content-based early exits used by normal equality.
    # UTF-8 bytes also prevent non-ASCII input from causing a comparison error.
    if not secrets.compare_digest(
        event.secret_token.encode("utf-8"),
        configured_secret.get_secret_value().encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook secret",
        )

    # 3. Reject live events during sandbox development.
    if event.live:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Live payments are not accepted",
        )

    # 4. Log selected metadata only, never the full payload, card data, or secret.
    payment_id = event.data.get("id")
    payment_status = event.data.get("status")

    # Ignore nested values and escape newlines so input cannot forge log lines.
    metadata = {
        "event_id": event.id[:128],
        "type": event.type[:128],
        "payment_id": payment_id[:128] if isinstance(payment_id, str) else None,
        "status": payment_status[:64] if isinstance(payment_status, str) else None,
    }
    logger.info("+++++++++++++++=========🚀===========+++++++++++++++++++")
    logger.info("Moyasar webhook %s", json.dumps(metadata))

    # 5. Test-only acknowledgement: received does NOT mean saved or paid.
    # Production: durably save/deduplicate the event BEFORE returning 200.
    # Logging alone can lose an event if the server crashes after this response.
    return {"received": True}
