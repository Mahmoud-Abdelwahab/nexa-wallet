from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.topup import TopUp


class TopUpRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, topup: TopUp) -> TopUp:
        self.db.add(topup)
        self.db.flush()
        return topup

    def get_by_id_for_user(
        self,
        topup_id: UUID,
        user_id: int,
    ) -> TopUp | None:

        stmt = select(TopUp).where(
            TopUp.id == topup_id,
            TopUp.user_id == user_id,
        )

        return self.db.scalar(stmt)

# we need this later when the webhook arrives to identify the TopUp associated with the payment.
    def get_by_payment_id(
        self,
        payment_id: str,
    ) -> TopUp | None:

        stmt = select(TopUp).where(
            TopUp.moyasar_payment_id == payment_id
        )

        return self.db.scalar(stmt)