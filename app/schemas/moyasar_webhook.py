from typing import Any

from pydantic import BaseModel

# Validate the webhook body before the route processes it.
# The route/service must also verify the secret; this schema only validates fields.
class MoyasarWebhookEvent(BaseModel):
    """Input schema for a notification received from Moyasar.

    Source: the JSON body sent to POST /webhooks/moyasar.
    Use: validate fields before the route verifies the secret and saves a record.
    This object contains the incoming payload; it is not stored as a DB row.
    """

    # Event ID: identifies this notification, not the payment itself.
    id: str
    # Event type: what happened, for example "payment_paid".
    type: str
    # Shared secret: compare with our configured webhook secret before processing.
    secret_token: str
    # True = live payment mode; False = test payment mode.
    live: bool | None = None
    # Payment details, including the Payment ID in data["id"].
    data: dict[str, Any]
