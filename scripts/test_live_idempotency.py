"""Opt-in local integration test; creates and retains two disposable test accounts.

Run with the local API/Postgres/Redis running:
    DEBUG=false .venv/bin/python scripts/test_live_idempotency.py --run-local

Seeds opening balances directly for test fixtures, NOT through a real top-up.
Never use this script against production. Tokens/passwords are not printed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
import secrets
import sys
from threading import Barrier
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text
from app.database import SessionLocal, engine
from app.models import LedgerEntry, Transaction, Wallet
from app.models.idempotency_record import IdempotencyRecord, IdempotencyStatus

BASE_URL = 'http://127.0.0.1:8000'


def api(method, path, body=None, token=None, key=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if key:
        headers['Idempotency-Key'] = key
    request = Request(BASE_URL + path, method=method, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
    try:
        response = urlopen(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def balances():
    with SessionLocal() as db:
        return dict(db.execute(select(Wallet.id, Wallet.balance)).all())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-local', action='store_true', required=True)
    parser.parse_args()
    assert engine.url.get_backend_name() == 'postgresql'
    assert engine.url.host in ('localhost', '127.0.0.1'), 'Local database only'
    original = balances()
    wallets = []
    sender_token = None
    sender_uid = None
    run_id = uuid4().hex[:12]

    # Register fresh users, then explicitly log in to obtain the access token.
    for role in ('sender', 'receiver'):
        email = f'idempotency.{run_id}.{role}@example.com'
        password = secrets.token_urlsafe(24)
        status, _ = api('POST', '/auth/register', {
            'username': f'idem_{role}_{run_id}', 'email': email,
            'password': password, 'mobile': str(uuid4().int)[:18],
            'date_of_birth': '2000-01-01',
        })
        assert status == 201, f'Registration status: {status}'
        status, login = api('POST', '/auth/login', {'email': email, 'password': password})
        assert status == 200, f'Login status: {status}'
        token = login['access_token']
        status, result = api('GET', '/wallets', token=token)
        assert status == 200
        wallets.append(next(w['id'] for w in result if w['currency'] == 'AED'))
        if role == 'sender':
            sender_token, sender_uid = token, login['user']['id']

    sender, receiver = wallets
    # Only newly created fixtures receive synthetic opening balances.
    with SessionLocal.begin() as db:
        db.get(Wallet, sender).balance = Decimal('500')
        db.get(Wallet, receiver).balance = Decimal('100')

    body = {'sender_wallet_id': sender, 'receiver_wallet_id': receiver, 'amount': '100.0000'}

    def verify(key, transaction_id, expected_sender, expected_receiver):
        with SessionLocal() as db:
            records = list(db.scalars(select(IdempotencyRecord).where(
                IdempotencyRecord.user_id == sender_uid,
                IdempotencyRecord.idempotency_key == key)))
            assert len(records) == 1
            assert records[0].status == IdempotencyStatus.COMPLETED
            assert records[0].transaction_id == transaction_id
            tx = db.get(Transaction, transaction_id)
            assert tx.status.value == 'SUCCESS' and tx.amount == Decimal('100')
            entries = list(db.scalars(select(LedgerEntry).where(
                LedgerEntry.transaction_id == transaction_id)))
            assert len(entries) == 2
            assert {(e.wallet_id, e.direction.value, e.amount, e.entry_type.value)
                    for e in entries} == {
                (sender, 'DEBIT', Decimal('100'), 'TRANSFER'),
                (receiver, 'CREDIT', Decimal('100'), 'TRANSFER'),
            }
        current = balances()
        assert current[sender] == Decimal(expected_sender)
        assert current[receiver] == Decimal(expected_receiver)
        assert all(current[wid] == balance for wid, balance in original.items())

    # Same key and body replays; changed amount must conflict without posting.
    first = api('POST', '/transfers', body, sender_token, 'TEST-001')
    replay = api('POST', '/transfers', body, sender_token, 'TEST-001')
    conflict = api('POST', '/transfers', {**body, 'amount': '200.0000'}, sender_token, 'TEST-001')
    assert [first[0], replay[0], conflict[0]] == [201, 201, 409]
    assert first[1] == replay[1]
    verify('TEST-001', first[1]['id'], '400', '200')

    # Hold the sender row so all three real HTTP requests overlap in PostgreSQL.
    # One request waits for this row; duplicate keys wait for that transaction.
    barrier = Barrier(3)
    def transfer():
        barrier.wait(timeout=10)
        return api('POST', '/transfers', body, sender_token, 'TEST-CONCURRENT-001')

    with ThreadPoolExecutor(max_workers=3) as pool:
        with SessionLocal.begin() as blocker:
            blocker.execute(select(Wallet).where(Wallet.id == sender).with_for_update())
            blocker_pid = blocker.scalar(text('select pg_backend_pid()'))
            futures = [pool.submit(transfer) for _ in range(3)]
            deadline = monotonic() + 10
            waiting = 0
            while monotonic() < deadline:
                # Count direct and indirect lock waiters rooted at our connection.
                with engine.connect() as monitor:
                    waiting = monitor.scalar(text('''
                        WITH RECURSIVE waiting(pid) AS (
                            SELECT pid FROM pg_stat_activity
                            WHERE :root = ANY(pg_blocking_pids(pid))
                            UNION
                            SELECT a.pid FROM pg_stat_activity a
                            JOIN waiting w ON w.pid = ANY(pg_blocking_pids(a.pid))
                        ) SELECT count(*) FROM waiting
                    '''), {'root': blocker_pid})
                if waiting >= 3:
                    break
                sleep(0.05)
            assert waiting >= 3, 'Did not observe three concurrent lock waiters'
        results = [future.result(timeout=35) for future in futures]

    assert [r[0] for r in results] == [201, 201, 201]
    assert all(r[1] == results[0][1] for r in results)
    verify('TEST-CONCURRENT-001', results[0][1]['id'], '300', '300')
    with SessionLocal() as db:
        transactions = list(db.scalars(select(Transaction).where(
            Transaction.sender_wallet_id == sender)))
        assert len(transactions) == 2, 'Unexpected extra transfer'
    print(json.dumps({
        'run_id': run_id, 'sender_wallet_id': sender, 'receiver_wallet_id': receiver,
        'sequential': {'statuses': [first[0], replay[0], conflict[0]],
                       'transaction_id': first[1]['id'], 'balances': ['400.0000', '200.0000']},
        'concurrent': {'statuses': [r[0] for r in results],
                       'transaction_ids': [r[1]['id'] for r in results],
                       'observed_lock_waiters': waiting, 'balances': ['300.0000', '300.0000']},
        'per_key': {'idempotency_records': 1, 'transactions': 1, 'ledger_entries': 2},
        'existing_wallet_balances_unchanged': True,
    }, indent=2))


if __name__ == '__main__':
    main()
