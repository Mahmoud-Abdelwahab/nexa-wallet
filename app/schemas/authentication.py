from datetime import date

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.password import Password


class RegisterRequest(BaseModel):

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


class LoginRequest(BaseModel):
    email: str
    password: str
