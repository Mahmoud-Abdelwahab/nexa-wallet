from datetime import date

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):

    username: str = Field(
        min_length=3,
        max_length=50,
    )

    password: str = Field(
        min_length=8,
        max_length=100,
    )

    mobile: str = Field(
        min_length=8,
        max_length=20,
    )

    date_of_birth: date



class RegisterResponse(BaseModel):

    id: int

    username: str

    mobile: str


class LoginRequest(BaseModel):

    username: str

    password: str


class TokenResponse(BaseModel):

    access_token: str

    token_type: str = "bearer"