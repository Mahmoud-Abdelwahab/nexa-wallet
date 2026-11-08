from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.topup import TopUp


class TopUpRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, topup: TopUp) -> TopUp:
        # Add and flush the request; the service owns the commit.
        self.db.add(topup)
        self.db.flush()
        return topup

    def get_by_key(self, user_id: int, key: str) -> TopUp | None:
        # Keys belong to a user; another user's key never reveals their request.
        return self.db.scalar(select(TopUp).where(
            TopUp.user_id == user_id, TopUp.idempotency_key == key,
        ))

    def create_once(self, topup: TopUp) -> TopUp:
        if topup.idempotency_key is None:
            raise ValueError("Idempotency key is required")
        # PostgreSQL waits for a competing insert with this user/key to finish.
        # If it commits, return its TopUp; if it rolls back, our insert can succeed.
        stmt = insert(TopUp).values(
            user_id=topup.user_id, wallet_id=topup.wallet_id,
            amount_minor=topup.amount_minor, currency=topup.currency,
            moyasar_payment_id=topup.moyasar_payment_id,
            idempotency_key=topup.idempotency_key, status=topup.status,
        ).on_conflict_do_nothing( # لو فيه سجل بالفعل بنفس المستخدم والمفتاح، ما تضيفش نسخة تانية وما تعدّلش السجل القديم.
            index_elements=["user_id", "idempotency_key"],
        ).returning(TopUp)
        created = self.db.scalar(stmt)
        if created is not None:
            return created
        existing = self.get_by_key(topup.user_id, topup.idempotency_key)
        if existing is None:
            raise RuntimeError("Top up result unavailable")
        return existing

    def get_by_id_for_user(
        self,
        topup_id: UUID,
        user_id: int,
    ) -> TopUp | None:

        # Match both request ID and owner so users cannot read another user's TopUp.
        stmt = select(TopUp).where(
            TopUp.id == topup_id,
            TopUp.user_id == user_id,
        )

        return self.db.scalar(stmt)

    # Find the funding request linked to this payment ID.
    def get_by_payment_id(
        self,
        payment_id: str,
    ) -> TopUp | None:

        stmt = select(TopUp).where(
            TopUp.moyasar_payment_id == payment_id
        )

        return self.db.scalar(stmt)
