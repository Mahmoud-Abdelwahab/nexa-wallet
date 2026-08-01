from pydantic_settings import BaseSettings, SettingsConfigDict

# This file used to read environment variables from .env file 
# for production we don't store keys in .env , we use aws secrets manager or other secure vaults to store keys and secrets.

class Settings(BaseSettings):
    app_name: str = "Nexa Wallet"
    debug: bool = True
    database_url: str = "postgresql+psycopg://nexa_user:nexa_password@localhost:5432/nexa_wallet"
    # SECRET_KEY: str = "your-secret-key-change-in-production"
    # ALGORITHM: str = "HS256"
    # ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


settings = Settings()