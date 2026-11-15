from pydantic import BaseModel, EmailStr, HttpUrl

from app.core.enums import Currency


class AboutResponse(BaseModel):
    app_name: str
    api_version: str
    description: str
    supported_currencies: list[Currency]


class SupportResponse(BaseModel):
    available: bool
    email: EmailStr | None
    url: HttpUrl | None
    message: str
