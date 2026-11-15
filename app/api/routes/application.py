from fastapi import APIRouter, Request

from app.core.config import settings
from app.core.enums import Currency
from app.schemas.application import AboutResponse, SupportResponse


router = APIRouter(prefix="/app", tags=["Application"])


@router.get("/about", response_model=AboutResponse)
async def get_about(request: Request) -> AboutResponse:
    # Public display data only. Explicit fields prevent configuration secrets
    # from leaking; this endpoint never serializes the Settings object itself.
    return AboutResponse(
        app_name=settings.app_name,
        api_version=request.app.version,
        description=settings.APP_DESCRIPTION,
        supported_currencies=list(Currency),
    )


@router.get("/support", response_model=SupportResponse)
async def get_support() -> SupportResponse:
    # Public so users can find support while signed out. Contacts come from
    # configuration; this does not create tickets or send messages to anyone.
    available = bool(settings.SUPPORT_EMAIL or settings.SUPPORT_URL)
    return SupportResponse(
        available=available,
        email=settings.SUPPORT_EMAIL,
        url=settings.SUPPORT_URL,
        message="Contact us using one of the options below." if available else "Support is currently unavailable.",
    )
