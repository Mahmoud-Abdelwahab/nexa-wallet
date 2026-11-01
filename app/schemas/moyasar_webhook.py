from typing import Any

from pydantic import BaseModel

# Validate the webhook body before the route processes it.
# The route/service must also verify the secret; this schema only validates fields.
class MoyasarWebhookEvent(BaseModel):
    # Event ID: identifies this notification, not the payment itself.
    id: str
    # Event type: what happened, for example "payment_paid".
    type: str
    # Shared secret: compare with our configured webhook secret before processing.
    secret_token: str
    # True = live payment mode; False = test payment mode.
    live: bool
    # Payment details, including the Payment ID in data["id"].
    data: dict[str, Any]
