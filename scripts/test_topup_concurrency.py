"""Opt-in PostgreSQL test: two workers must credit one payment only once.

Run: DEBUG=false .venv/bin/python scripts/test_topup_concurrency.py --run-local
Uses a temporary schema and fake Moyasar responses; removes its schema afterward.
"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from uuid import uuid4
from sqlalchemy import create_engine, select, func, text, inspect
from sqlalchemy.orm import Session, sessionmaker
from app.database import Base, engine
import app.models
from app.models import User, Wallet, TopUp, Transaction, LedgerEntry
from app.models.moyasar_webhook_event import MoyasarWebhookEventRecord as Event
from app.core.enums import Currency
from app.integrations.moyasar_client import MoyasarPayment
from app.workers.moyasar_worker import process_one
from app.repositories.payment_job_repository import PaymentJobRepository
from app.services.topup_payment_processor import TopUpPaymentProcessor
from app.services.topup_service import TopUpService, TopUpIdempotencyConflictError



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local", action="store_true", required=True)
    parser.parse_args()
    assert engine.url.host in ('localhost', '127.0.0.1')
    schema='audit_topup_'+uuid4().hex
    with engine.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    audit_engine=create_engine(engine.url,connect_args={'options':f'-csearch_path={schema}'})
    try:
        Base.metadata.create_all(audit_engine)
        with Session(audit_engine) as db, db.begin():
            u=User(username='audit',email='audit@example.com',mobile='123456789',date_of_birth=date(2000,1,1),password_hash='unused')
            db.add(u);db.flush()
            w=Wallet(user_id=u.id,currency=Currency.AED,balance=Decimal('50'))
            db.add(w);db.flush()
            t=TopUp(user_id=u.id,wallet_id=w.id,amount_minor=10000,currency=Currency.AED,moyasar_payment_id=str(uuid4()))
            db.add(t);db.flush()
            wid,tid,pid=w.id,t.id,t.moyasar_payment_id
            for i in range(2):
                db.add(Event(id=f'event-{i}',payment_id=pid,event_type='payment_paid',next_attempt_at=datetime.now(timezone.utc)-timedelta(minutes=1)))
        barrier=Barrier(2)
        def run():
            class FakeMoyasar:
                def get_payment(self, payment_id):
                    # Both claims are committed and connections released before this barrier.
                    barrier.wait(timeout=10)
                    with Session(audit_engine) as check, check.begin():
                        # An independent transaction can lock both events while HTTP is in flight.
                        # Use a short timeout to make accidental long-held locks fail the test.
                        check.execute(text("SET LOCAL lock_timeout = '2s'"))
                        list(check.scalars(select(Event).where(Event.payment_id == payment_id)
                             .order_by(Event.id).with_for_update()))
                    return MoyasarPayment(id=payment_id,status='paid',amount=10000,currency='AED')
            return process_one(FakeMoyasar(), sessionmaker(bind=audit_engine))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(run) for _ in range(2)]
            assert all(f.result(timeout=20) for f in futures)
        with Session(audit_engine) as db:
            assert db.get(Wallet,wid).balance==Decimal('150')
            assert db.get(TopUp,tid).status.value=='COMPLETED'
            assert db.scalar(select(func.count()).select_from(Transaction))==1
            assert db.scalar(select(func.count()).select_from(LedgerEntry))==1
            assert db.scalar(select(func.count()).select_from(Event).where(Event.processed_at.is_not(None)))==2
        print('PASS PostgreSQL: two concurrent workers, distinct events, same payment -> one transaction, one credit, balance 50 -> 150')

        def test_creation_race(key, amounts):
            # Force every request past the initial lookup before any INSERT runs.
            start_insert = Barrier(len(amounts))

            def create_request(amount):
                with Session(audit_engine) as db:
                    service = TopUpService(db)
                    create_once = service.topup_repository.create_once

                    def insert_together(topup):
                        start_insert.wait(timeout=10)
                        return create_once(topup)

                    service.topup_repository.create_once = insert_together
                    try:
                        result = service.create_topup(1, wid, Decimal(amount), key)
                        return (str(result.id), result.moyasar_payment_id)
                    except TopUpIdempotencyConflictError:
                        return 'conflict'

            with ThreadPoolExecutor(max_workers=len(amounts)) as pool:
                futures = [pool.submit(create_request, amount) for amount in amounts]
                results = [future.result(timeout=20) for future in futures]
            with Session(audit_engine) as db:
                assert db.scalar(select(func.count()).select_from(TopUp).where(
                    TopUp.idempotency_key == key,
                )) == 1
                assert db.get(Wallet, wid).balance == Decimal('150')
                assert db.scalar(select(func.count()).select_from(Transaction)) == 1
                assert db.scalar(select(func.count()).select_from(LedgerEntry)) == 1
            return results

        same = test_creation_race('same-request', ['3', '3.0', '3.00'])
        assert same[0] != 'conflict' and len(set(same)) == 1
        print('PASS PostgreSQL: three concurrent creations -> one TopUp and one Payment ID')
        changed = test_creation_race('changed-request', ['3', '4'])
        assert changed.count('conflict') == 1
        print('PASS PostgreSQL: concurrent changed data -> one winner, one conflict, no credit')

        with Session(audit_engine) as db, db.begin():
            pending = db.scalar(select(TopUp).where(TopUp.idempotency_key == 'same-request'))
            pending_id, pending_payment = pending.id, pending.moyasar_payment_id
            db.add(Event(id='lease-test', payment_id=pending_payment, event_type='reconciliation',
                         next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
        start_claim = Barrier(2)

        def claim():
            start_claim.wait(timeout=10)
            with Session(audit_engine) as db, db.begin():
                return PaymentJobRepository(db).claim_next()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = [pool.submit(claim) for _ in range(2)]
            jobs = [f.result(timeout=20) for f in claims]
        assert sum(job is not None for job in jobs) == 1
        old_job = next(job for job in jobs if job is not None)
        with Session(audit_engine) as db, db.begin():
            db.get(Event, 'lease-test').next_attempt_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        with Session(audit_engine) as db, db.begin():
            new_job = PaymentJobRepository(db).claim_next()
        assert new_job.attempt == old_job.attempt + 1
        response = MoyasarPayment(id=pending_payment, status='paid', amount=300, currency='AED')
        with Session(audit_engine) as db, db.begin():
            TopUpPaymentProcessor(db).finish_payment(old_job, response)
        with Session(audit_engine) as db:
            assert db.get(Wallet, wid).balance == Decimal('150')
        with Session(audit_engine) as db, db.begin():
            TopUpPaymentProcessor(db).finish_payment(new_job, response)
        with Session(audit_engine) as db:
            assert db.get(Wallet, wid).balance == Decimal('153')
            assert db.get(TopUp, pending_id).status.value == 'COMPLETED'
        print('PASS PostgreSQL: one claimant, expired lease reclaimed, stale worker cannot credit')
    finally:
        audit_engine.dispose()
        with engine.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        print('Removed isolated audit schema; existing data untouched. Moyasar API was mocked.')


if __name__ == "__main__":
    main()
