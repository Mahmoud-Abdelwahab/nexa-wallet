from pydantic import EmailStr, Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# This file used to read environment variables from .env file
# for production we don't store keys in .env , we use aws secrets manager or other secure vaults to store keys and secrets.


class Settings(BaseSettings):
    app_name: str = "Nexa Wallet"
    APP_DESCRIPTION: str = "Manage your wallets, send transfers, and review your transaction history."
    SUPPORT_EMAIL: EmailStr | None = None
    SUPPORT_URL: HttpUrl | None = None
    debug: bool = True
    # database_url: str = "postgresql+psycopg://nexa_user:nexa_password@localhost:5432/nexa_wallet"
    database_url: str = "postgresql+psycopg://nexa:nexa_password@localhost:5433/nexa_wallet_dev"
    secret_key: str
    # Read from .env; SecretStr hides the value when settings are printed.
    MOYASAR_WEBHOOK_SECRET: SecretStr | None = None
    # Backend API key for fetching payments; separate from the webhook secret.
    MOYASAR_SECRET_KEY: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    REDIS_URL: str = "redis://localhost:6379/0"
    REFRESH_TOKEN_IDLE_DAYS: int = 30
    REFRESH_TOKEN_ABSOLUTE_DAYS: int = 90
    RECIPIENT_LOOKUP_DAILY_LIMIT: int = Field(default=50, gt=0)
    SMTP_HOST: str = "127.0.0.1"
    SMTP_PORT: int = 1025
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: SecretStr = SecretStr("")
    SMTP_FROM: str = "nexa@local.test"
    SMTP_STARTTLS: bool = False
    MOYASAR_TOPUP_ALLOWED_CURRENCIES: str = "SAR,AED"

    @field_validator("SUPPORT_EMAIL", "SUPPORT_URL", mode="before")
    @classmethod
    def empty_support_contact_is_unconfigured(cls, value):
        # Environment templates leave optional public contacts blank.
        return None if isinstance(value, str) and not value.strip() else value
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

# BaseSettings reads required fields from environment variables and .env at runtime.
# Pylance cannot see those values; suppress its missing-arguments warning on this call only.
# Fields remain required: missing configuration still raises ValidationError at startup.
settings = Settings()  # pyright: ignore[reportCallIssue]
