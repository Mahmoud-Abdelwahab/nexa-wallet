"""CSV download for one authenticated wallet. No extra provider or DB table."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.routes.dependencies import get_current_user
from app.core.errors import BusinessError
from app.database import get_db
from app.models.user import User
from app.services.wallet_statement_service import (
    InvalidStatementPeriod,
    StatementTooLarge,
    WalletStatementService,
)


router = APIRouter()  # Included under the existing /wallets parent router.


@router.get("/{wallet_id}/statement", response_class=Response)
def download_wallet_statement(
    wallet_id: int,
    start_date: date | None = None,
    end_date: date | None = None,
    format: Literal["csv"] = "csv",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # This is a sync handler because the project uses a synchronous DB Session.
    service = WalletStatementService(db)
    try:
        from_day, to_day = service.normalize_period(start_date, end_date)
        csv_content = service.export_csv(
            user_id=current_user.id,
            wallet_id=wallet_id,
            from_day=from_day,
            to_day=to_day,
        )
    except InvalidStatementPeriod:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid date range (max 366 calendar days, not in the future)",
        )
    except BusinessError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Wallet not found",
        )
    except StatementTooLarge:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Too many entries; choose a shorter date range",
        )

    filename = f"nexa_wallet_{wallet_id}_{from_day}_{to_day}.csv"
    return Response(
        content=csv_content.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
