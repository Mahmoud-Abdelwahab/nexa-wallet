import httpx
from pydantic import BaseModel

from app.core.config import settings


class MoyasarPayment(BaseModel):
    id: str
    status: str
    amount: int
    currency: str


class MoyasarClient:
    BASE_URL = "https://api.moyasar.com/v1"

    # Business — Step 4: fetch payment details before deciding to credit a wallet.
    # Worker -> GET /payments/{id} -> status, amount, currency -> caller verifies.
    # This reads an existing payment; it does not create a payment or charge again.
    def get_payment(self, payment_id: str) -> MoyasarPayment:
        # Sync HTTP fits our planned sync worker and SQLAlchemy Session workflow.
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
            return MoyasarPayment.model_validate(
                response.json()
            )
