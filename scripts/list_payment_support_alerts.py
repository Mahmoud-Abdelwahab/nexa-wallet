"""Local support view: python scripts/list_payment_support_alerts.py [--limit 50].

Reads persisted alerts only. Does not retry payments, clear alerts or change money.
No public support endpoint exists; access requires local database credentials.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from app.database import SessionLocal
from app.models.moyasar_webhook_event import MoyasarWebhookEventRecord as Event
from app.models.topup import TopUp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("limit must be between 1 and 1000")
    # Durable alerts remain visible after the worker/server restarts.
    with SessionLocal() as db:
        rows = db.execute(select(Event, TopUp).outerjoin(
            TopUp, TopUp.moyasar_payment_id == Event.payment_id,
        ).where(Event.support_alert_at.is_not(None)).order_by(
            Event.support_alert_at.desc(), Event.id,
        ).limit(args.limit)).all()
        for event, topup in rows:
            print(json.dumps({
                "event_id": event.id, "payment_id": event.payment_id,
                "topup_id": str(topup.id) if topup else None,
                "status": topup.status.value if topup else None,
                "attempts": event.attempts, "reason": event.last_error,
                "alert_at": event.support_alert_at.isoformat(),
            }))
        if not rows:
            print("No payment support alerts.")


if __name__ == "__main__":
    main()
