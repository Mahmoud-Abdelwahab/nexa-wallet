import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings


class MoyasarPayment(BaseModel):
    """Response schema for a payment fetched directly from Moyasar's API.

    Source: GET /v1/payments/{payment_id}, requested by MoyasarClient.
    Use: TopUpPaymentProcessor compares these fields with the saved TopUp.
    This is an in-memory response object, not a database table or webhook event.
    """

    # Moyasar Payment ID; must match the event and TopUp's expected payment ID.
    id: str = Field(min_length=1, max_length=100)
    # Moyasar payment state, such as paid; different from Nexa's TopUpStatus.
    status: str = Field(min_length=1, max_length=64)
    # Integer in minor units; compare directly with TopUp.amount_minor.
    amount: int = Field(gt=0)
    # Payment currency returned by Moyasar; compare with TopUp.currency.
    currency: str = Field(min_length=3, max_length=3)

    # Reject wrong JSON types instead of silently converting money values.
    model_config = ConfigDict(strict=True)


class MoyasarInvalidResponseError(Exception):
    """The payment lookup returned unusable JSON or fields; no credit is allowed."""


class MoyasarClient:
    BASE_URL = "https://api.moyasar.com/v1"

    @staticmethod
    def validate_configuration() -> None:
        # Fail before processing events if the sandbox API key is not configured.
        # This checks configuration only; Moyasar still verifies the real key.
        key = settings.MOYASAR_SECRET_KEY
        if (
            not key.startswith("sk_test_")
            or not key.removeprefix("sk_test_")
            or "YOUR_KEY" in key
        ):
            raise RuntimeError("Configure a real Moyasar test API key before starting the worker")

    # Business — Step 4: fetch payment details before deciding to credit a wallet.
    # Worker -> GET /payments/{id} -> status, amount, currency -> caller verifies.
    # This reads an existing payment; it does not create a payment or charge again.
    def get_payment(self, payment_id: str) -> MoyasarPayment:
        self.validate_configuration()
        # Sync HTTP fits our worker and SQLAlchemy Session workflow.
        # Client blocks its calling thread; it does not start a worker automatically.
        # Run this processing in the separate worker, outside the FastAPI request.
        with httpx.Client(timeout=8.0) as client:
            response = client.get(
                f"{self.BASE_URL}/payments/{payment_id}",
                # HTTP Basic Auth: username = secret API key, password = empty.
                auth=(settings.MOYASAR_SECRET_KEY, ""),
            )

            # Reject HTTP error responses before reading payment details.
            response.raise_for_status()

            # Parse the payment ID, status, amount (minor units), and currency.
            try:
                return MoyasarPayment.model_validate(response.json())
            except ValueError:
                # JSON decoding and Pydantic validation errors must be retryable.
                # Do not include the response body, card data, or secret in the error.
                raise MoyasarInvalidResponseError("Invalid Moyasar payment response") from None
