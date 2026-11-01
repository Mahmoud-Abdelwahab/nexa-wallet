from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# This file used to read environment variables from .env file
# for production we don't store keys in .env , we use aws secrets manager or other secure vaults to store keys and secrets.


class Settings(BaseSettings):
    app_name: str = "Nexa Wallet"
    debug: bool = True
    # database_url: str = "postgresql+psycopg://nexa_user:nexa_password@localhost:5432/nexa_wallet"
    database_url: str = "postgresql+psycopg://nexa:nexa_password@localhost:5433/nexa_wallet_dev"
    secret_key: str
    # Read from .env; SecretStr hides the value when settings are printed.
    MOYASAR_WEBHOOK_SECRET: SecretStr | None = None
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    REDIS_URL: str = "redis://localhost:6379/0"
    REFRESH_TOKEN_IDLE_DAYS: int = 30
    REFRESH_TOKEN_ABSOLUTE_DAYS: int = 90
    MOYASAR_TOPUP_ALLOWED_CURRENCIES: str = "AED,SAR"
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

settings = Settings()
