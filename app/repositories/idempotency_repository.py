from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.idempotency_record import (
    IdempotencyRecord,
    IdempotencyStatus,
)


class IdempotencyRepository:

    def __init__(self, db: Session):
        self.db = db

    def try_reserve(
        self,
        user_id: int,
        key: str,
        request_hash: str,
    ) -> bool:

        stmt = (
            insert(IdempotencyRecord)
            .values(
                user_id=user_id,
                idempotency_key=key,
                request_hash=request_hash,
                status=IdempotencyStatus.PROCESSING,
            )
            .on_conflict_do_nothing(
                constraint="uq_idempotency_user_key"
            )
            .returning(IdempotencyRecord.id)
        )

        record_id = self.db.scalar(stmt)

        # New row inserted -> RETURNING gives its id -> True (key reserved).
        # Existing user/key -> #! ON CONFLICT DO NOTHING returns no row -> None
        # -> False (the service must check the saved hash and result).
        return record_id is not None

    def get_by_key(
        self,
        user_id: int,
        key: str,
    ) -> IdempotencyRecord | None:

        stmt = select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.idempotency_key == key,
        )

        return self.db.scalar(stmt)
