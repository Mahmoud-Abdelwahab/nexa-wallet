from datetime import date

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.password import Password
from app.schemas.device_session import DeviceMetadata


class RegisterRequest(DeviceMetadata):
    # Inherit optional device metadata and its validation from DeviceMetadata without duplicating fields.
    # Registration also logs the user in, so the first session needs device metadata.
    username: str = Field(
        min_length=3,
        max_length=50,
    )

    email: EmailStr

    password: Password

    mobile: str = Field(
        min_length=8,
        max_length=20,
    )

    date_of_birth: date



class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: EmailStr
    mobile: str


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: UserResponse


class LoginRequest(DeviceMetadata):
    # Older clients can send email/password only; device fields have defaults.
    email: str
    password: str
