from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.password import Password


class ForgotPasswordRequest(BaseModel):
    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    new_password: Password


class PasswordResetResponse(BaseModel):
    message: str
