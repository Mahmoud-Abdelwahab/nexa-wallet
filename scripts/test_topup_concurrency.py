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
from sqlalchemy.orm import Session
from app.database import Base, engine
import app.models
from app.models import User, Wallet, TopUp, Transaction, LedgerEntry
from app.models.moyasar_webhook_event import MoyasarWebhookEventRecord as Event
from app.core.enums import Currency
from app.integrations.moyasar_client import MoyasarPayment
from app.services.topup_payment_processor import TopUpPaymentProcessor



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
            with Session(audit_engine) as db,db.begin():
                processor=TopUpPaymentProcessor(db)
                def fetch(payment_id):
                    # Both workers hold separate event locks before posting begins.
                    barrier.wait(timeout=10)
                    return MoyasarPayment(id=payment_id,status='paid',amount=10000,currency='AED')
                processor.moyasar.get_payment=fetch
                return processor.process_next()
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
    finally:
        audit_engine.dispose()
        with engine.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        print('Removed isolated audit schema; existing data untouched. Moyasar API was mocked.')


if __name__ == "__main__":
    main()
